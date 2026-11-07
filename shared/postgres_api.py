"""
Postgres storage backend for FGC Check-in System.

Primary storage backend for FGC Check-in System (default).
Shares public function signatures with airtable_api.py (legacy fallback).

Connection uses DATABASE_URL from environment, with a connection pool
managed by psycopg3.
"""

import hmac
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- Feature flags ---
CANONICAL_PLAYER_ID_ENABLED = os.getenv("CANONICAL_PLAYER_ID_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
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
        from shared.migrate import apply_if_enabled

        try:
            apply_if_enabled(_pool)
        except Exception:
            _pool = None
            raise
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
                    cur.execute(f"ALTER TABLE event_stats ADD COLUMN IF NOT EXISTS {col} {typ}")
                # Canonical player ID column on active_event_data (added 2026-03-10)
                cur.execute(
                    "ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS player_uuid TEXT"
                )
                cur.execute(
                    "ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS added_via TEXT DEFAULT 'unknown'"
                )
                cur.execute(
                    "ALTER TABLE active_event_data ADD COLUMN IF NOT EXISTS acquisition_source TEXT"
                )
                cur.execute(
                    "UPDATE active_event_data SET added_via = 'unknown' WHERE added_via IS NULL"
                )
                for col in ["checkin_opened_at", "event_started_at", "event_ended_at"]:
                    cur.execute(f"ALTER TABLE settings ADD COLUMN IF NOT EXISTS {col} TIMESTAMPTZ")
                cur.execute(
                    "ALTER TABLE settings ADD COLUMN IF NOT EXISTS collect_acquisition_source BOOLEAN DEFAULT false"
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
                    "ALTER TABLE event_archive ADD COLUMN IF NOT EXISTS acquisition_source TEXT"
                )
                cur.execute(
                    "UPDATE event_archive SET added_via = 'unknown' WHERE added_via IS NULL"
                )

                # Merge log table (added 2026-03-11)
                cur.execute(
                    """
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
                """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_merge_log_keep ON merge_log(keep_uuid)")
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_merge_log_remove ON merge_log(remove_uuid)"
                )
        logger.info(
            "✅ Schema migrations checked (no-show + player_uuid + added_via + acquisition source + live ops timestamps + merge_log)"
        )
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


def _normalize_acquisition_source(value: Any) -> Optional[str]:
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if not candidate:
        return None
    aliases = {
        "startgg": "startgg",
        "start.gg": "startgg",
        "social media": "social",
        "social_media": "social",
    }
    candidate = aliases.get(candidate, candidate)
    allowed = {"friend", "discord", "startgg", "social", "venue", "other"}
    return candidate if candidate in allowed else "other"


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
        "member_lookup_state": row.get("member_lookup_state") or "unknown",
        "startgg": row.get("startgg"),
        "is_guest": row.get("is_guest"),
        "status": row.get("status"),
        "tournament_games_registered": row.get("tournament_games_registered"),
        "UUID": row.get("checkin_uuid"),
        "event_slug": row.get("event_slug"),
        "startgg_event_id": row.get("startgg_event_id"),
        "external_id": row.get("external_id"),
        "added_via": row.get("added_via"),
        "acquisition_source": row.get("acquisition_source"),
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

    # Slug-only selection must not carry names from a different event.
    if fields and "active_event_slug" in fields:
        for name_field in ("event_display_name", "tournament_name"):
            if name_field not in fields:
                columns.append(
                    f"{name_field} = CASE WHEN active_event_slug IS DISTINCT FROM %s "
                    f"THEN NULL ELSE {name_field} END"
                )
                values.append(_settings_value("active_event_slug", fields["active_event_slug"]))

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
    return {
        "record_id": str(data.get("id")),
        "fields": {k: v for k, v in data.items() if k != "id"},
    }


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
               added_via, acquisition_source, member_lookup_state
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
            acquisition_source,
            member_lookup_state,
        ) = row

        result.append(
            {
                "record_id": record_id,
                "created": created.isoformat() if created else None,
                "event_slug": event_slug,
                "status": status,
                "member": member,
                "member_lookup_state": member_lookup_state or "unknown",
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
                "acquisition_source": acquisition_source,
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
            cur.execute(
                "SELECT DISTINCT event_slug FROM active_event_data WHERE event_slug IS NOT NULL"
            )
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
               startgg_event_id, external_id, is_guest, added_via, acquisition_source, created
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
        "acquisition_source",
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
               startgg_event_id, external_id, is_guest, added_via, acquisition_source, created
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
        "acquisition_source",
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
               startgg_event_id, external_id, is_guest, added_via, acquisition_source, created,
               member_lookup_state
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
        "acquisition_source",
        "created",
        "member_lookup_state",
    ]
    row_dict = _row_to_dict(columns, row)
    return {"record_id": row_dict.get("record_id"), "fields": _checkin_fields_from_row(row_dict)}


def _update_checkin_with_status(
    conn, record_id: str, fields: Dict[str, Any], *, event_slug: Optional[str] = None
):
    """Lock the current row and persist fields plus derived status in one transaction."""
    with conn.cursor() as cur:
        where_sql = "record_id = %s"
        where_values = [record_id]
        if event_slug is not None:
            where_sql += " AND event_slug = %s"
            where_values.append(event_slug)
        cur.execute(f"SELECT * FROM active_event_data WHERE {where_sql} FOR UPDATE", where_values)
        row = cur.fetchone()
        if not row:
            return None
        current = _row_to_dict([desc[0] for desc in cur.description], row)

        cur.execute(
            "SELECT require_membership, require_payment, require_startgg "
            "FROM settings WHERE is_active = true ORDER BY id DESC LIMIT 1"
        )
        settings_row = cur.fetchone()
        settings = (
            _row_to_dict([desc[0] for desc in cur.description], settings_row)
            if settings_row
            else {}
        )
        update_fields = {
            "checkin_uuid" if key == "UUID" else key: value
            for key, value in (fields or {}).items()
            if key != "status"
        }
        update_fields["status"] = compute_checkin_status({**current, **update_fields}, settings)[
            "status"
        ]
        set_sql = ", ".join(f"{key} = %s" for key in update_fields)
        cur.execute(
            f"UPDATE active_event_data SET {set_sql} WHERE {where_sql} RETURNING *",
            [*update_fields.values(), *where_values],
        )
        row = cur.fetchone()
        if not row:
            return None
        row_dict = _row_to_dict([desc[0] for desc in cur.description], row)

    result_fields = _checkin_fields_from_row(row_dict)
    result_fields["player_uuid"] = row_dict.get("player_uuid")
    return {"record_id": row_dict["record_id"], "fields": result_fields}


def update_checkin(
    record_id: str,
    fields: Dict[str, Any],
    typecast: bool = False,
    member_source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Update a checkin and derive its status from the locked stored row."""
    if not record_id:
        return None
    with _get_pool().connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            # Discover the event without a row lock, then follow the event -> row order.
            cur.execute(
                "SELECT event_slug FROM active_event_data WHERE record_id = %s", (record_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            event_slug = row[0]
            slugs = {event_slug}
            target_slug = (fields or {}).get("event_slug")
            if isinstance(target_slug, str):
                slugs.add(target_slug)
            for slug in sorted(slugs):
                cur.execute(
                    "SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (slug,)
                )
            cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
            cur.execute(
                "SELECT record_id FROM active_event_data "
                "WHERE record_id = %s AND event_slug = %s FOR UPDATE",
                (record_id, event_slug),
            )
            if not cur.fetchone():
                return None
        if "member" in (fields or {}):
            provenance = member_source or fields.get("member_source") or "to"
            if provenance not in ("to", "v2", "ebas_check"):
                raise ValueError("Invalid membership source")
            fields = {**fields, "member_source": provenance}
        result = _update_checkin_with_status(conn, record_id, fields, event_slug=event_slug)
        if result and "member" in (fields or {}) and fields.get("member_source") == "to":
            _resolve_legacy_registration_by_to(conn, record_id, event_slug, fields["member"])
        return result


def recompute_active_statuses(event_slug: str) -> int:
    """Recompute one active event atomically through the canonical status update path."""
    if not event_slug:
        raise ValueError("event_slug is required")
    with _get_pool().connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            # Serialize the batch with check-in creation's shared event lock.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))",
                (event_slug,),
            )
            # Keep the requirement snapshot stable throughout the batch.
            cur.execute(
                "SELECT id FROM settings WHERE is_active = true ORDER BY id DESC LIMIT 1 FOR SHARE"
            )
            cur.fetchone()
            cur.execute(
                "SELECT record_id FROM active_event_data WHERE event_slug = %s "
                "ORDER BY record_id FOR UPDATE",
                (event_slug,),
            )
            record_ids = [row[0] for row in cur.fetchall()]
        for record_id in record_ids:
            if not _update_checkin_with_status(conn, record_id, {}):
                raise RuntimeError(
                    "An active check-in disappeared during status recomputation"
                )
    return len(record_ids)


def delete_checkin(record_id: str) -> bool:
    """Delete a checkin record."""
    if not record_id:
        return False

    with _get_pool().connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_slug FROM active_event_data WHERE record_id = %s", (record_id,)
            )
            row = cur.fetchone()
            if not row:
                return False
            event_slug = row[0]
            cur.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (event_slug,)
            )
            cur.execute(
                "DELETE FROM active_event_data WHERE record_id = %s AND event_slug = %s",
                (record_id, event_slug),
            )
            return cur.rowcount > 0


def _ownership_phone(value):
    if not isinstance(value, str):
        return ""
    digits = re.sub(r"[^0-9]", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = "46" + digits[1:]
    return digits


def _contact_ownership_verified(payload, email, telephone):
    submitted_phone = _ownership_phone(payload.get("telephone"))
    stored_phone = _ownership_phone(telephone)
    submitted_email = payload.get("email")
    submitted_email = submitted_email.strip().lower() if isinstance(submitted_email, str) else ""
    stored_email = email.strip().lower() if isinstance(email, str) else ""
    phone_matches = bool(submitted_phone and stored_phone) and hmac.compare_digest(
        submitted_phone.encode("utf-8"), stored_phone.encode("utf-8")
    )
    email_matches = bool(submitted_email and stored_email) and hmac.compare_digest(
        submitted_email.encode("utf-8"), stored_email.encode("utf-8")
    )
    return phone_matches or email_matches


def _begin_existing_checkin(conn, event_slug, checkin_id, payload, *, public):
    """Compare ownership with contacts from the already-locked, pre-update row."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email, telephone, acquisition_source FROM active_event_data "
            "WHERE record_id = %s AND event_slug = %s FOR UPDATE",
            (checkin_id, event_slug),
        )
        row = cur.fetchone()
        if not row:
            return None
    ownership_verified = not public or _contact_ownership_verified(payload, row[0], row[1])
    safe_fields = {}
    if ownership_verified:
        for key in ("name", "email", "telephone"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                safe_fields[key] = value.strip()
    source = _normalize_acquisition_source(payload.get("acquisition_source"))
    if source is not None and (ownership_verified or not row[2]):
        safe_fields["acquisition_source"] = source
    updated = _update_checkin_with_status(conn, checkin_id, safe_fields, event_slug=event_slug)
    if not updated:
        return None
    return {
        "checkin_id": checkin_id,
        "record_id": checkin_id,
        "event_slug": event_slug,
        "created": False,
        "player_uuid": updated["fields"].get("player_uuid"),
        "ownership_verified": ownership_verified,
    }


def begin_checkin(
    event_slug: str, payload: Dict[str, Any], *, public: bool = False
) -> Dict[str, Any]:
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

    with _get_pool().connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (event_slug,)
            )
            cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
            existing = None
            if tag or name:
                column = "tag" if tag else "name"
                cur.execute(
                    f"SELECT record_id FROM active_event_data "
                    f"WHERE LOWER({column}) = LOWER(%s) AND event_slug = %s "
                    "ORDER BY created DESC LIMIT 1 FOR UPDATE",
                    (tag or name, event_slug),
                )
                existing = cur.fetchone()

            if existing:
                result = _begin_existing_checkin(
                    conn, event_slug, existing[0], payload, public=public
                )
                if result:
                    return result

            games = payload.get("tournament_games_registered")
            if isinstance(games, str):
                games = [g.strip() for g in games.split(",") if g.strip()]
            if not isinstance(games, list):
                games = []

            # Resolve player_uuid early (match-only, no creation)
            matched_player_uuid = None
            if CANONICAL_PLAYER_ID_ENABLED:
                try:
                    matched_player_uuid = _find_player_uuid(tag, payload.get("email"), cur=cur)
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

            normalized_source = _normalize_acquisition_source(payload.get("acquisition_source"))
            if normalized_source is not None:
                fields["acquisition_source"] = normalized_source

            cur.execute(
                "SELECT require_membership, require_payment, require_startgg "
                "FROM settings WHERE is_active = true ORDER BY id DESC LIMIT 1"
            )
            settings_row = cur.fetchone()
            settings = (
                _row_to_dict([desc[0] for desc in cur.description], settings_row)
                if settings_row
                else {}
            )
            fields["status"] = compute_checkin_status(fields, settings)["status"]
            candidate_id = str(uuid.uuid4())
            conflict_update = (
                "tag = active_event_data.tag"
                if public
                else """
                name = COALESCE(NULLIF(BTRIM(EXCLUDED.name), ''), active_event_data.name),
                email = COALESCE(NULLIF(BTRIM(EXCLUDED.email), ''), active_event_data.email),
                telephone = COALESCE(NULLIF(BTRIM(EXCLUDED.telephone), ''), active_event_data.telephone),
                acquisition_source = COALESCE(NULLIF(EXCLUDED.acquisition_source, ''),
                                              active_event_data.acquisition_source)
            """
            )
            cur.execute(
                f"""
                INSERT INTO active_event_data (
                    record_id, event_slug, external_id,
                    name, tag, email, telephone,
                    status, member, startgg, payment_valid,
                    payment_amount, payment_expected,
                    tournament_games_registered, checkin_uuid,
                    startgg_event_id, is_guest, added_via, acquisition_source, player_uuid
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON CONFLICT (event_slug, lower(tag)) DO UPDATE SET {conflict_update}
                RETURNING record_id
                """,
                (
                    candidate_id,
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
                    fields.get("acquisition_source"),
                    matched_player_uuid,
                ),
            )
            checkin_id = cur.fetchone()[0]
            if public and checkin_id != candidate_id:
                result = _begin_existing_checkin(conn, event_slug, checkin_id, payload, public=True)
                if not result:
                    raise RuntimeError("Check-in changed during ownership verification")
                return result
            _update_checkin_with_status(conn, checkin_id, {})

    return {
        "checkin_id": checkin_id,
        "record_id": checkin_id,
        "event_slug": event_slug,
        "created": True,
        "player_uuid": matched_player_uuid,
        "ownership_verified": True,
    }


def _integration_audit(conn, action, table, event_slug, checkin_id, details):
    """Keep callback/member audit evidence in the same transaction as the write."""
    conn.execute(
        """INSERT INTO audit_log
           (user_id, user_name, action, target_table, target_event, target_record, details)
           VALUES ('integration', 'integration', %s, %s, %s, %s, %s)""",
        (action, table, event_slug, checkin_id, json.dumps(details)),
    )


def _lock_integration_checkin(conn, checkin_id):
    """Acquire the event lock before the row lock, matching archive's lock order."""
    slug = conn.execute(
        "SELECT event_slug FROM active_event_data WHERE record_id = %s", (checkin_id,)
    ).fetchone()
    if not slug:
        return None
    conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (slug[0],))
    conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM active_event_data WHERE record_id = %s AND event_slug = %s FOR UPDATE",
            (checkin_id, slug[0]),
        )
        row = cur.fetchone()
        return _row_to_dict([column[0] for column in cur.description], row) if row else None


def start_integration_attempt(checkin_id: str, sources=("ebas", "startgg")) -> str:
    """Record one backend-generated run ID for both sources before sending n8n work."""
    attempt_id = str(uuid.uuid4())
    sources = tuple(dict.fromkeys(sources))
    if not sources or any(source not in ("ebas", "startgg") for source in sources):
        raise ValueError("Invalid integration sources")
    with _get_pool().connection() as conn, conn.transaction():
        if not _lock_integration_checkin(conn, checkin_id):
            raise ValueError("Check-in is no longer active")
        assignments = []
        for source in sources:
            assignments.extend(
                (
                    f"last_{source}_attempt_id = %s",
                    f"last_{source}_attempt_started_at = clock_timestamp()",
                    f"last_{source}_result_at = NULL",
                )
            )
        conn.execute(
            "UPDATE active_event_data SET " + ", ".join(assignments) + " WHERE record_id = %s",
            [*[attempt_id for _ in sources], checkin_id],
        )
    return attempt_id


def _lock_legacy_registration_owner(conn, checkin_id):
    """Event -> merge -> active/archive row -> attempt, including a late completion."""
    found = conn.execute(
        "SELECT event_slug FROM active_event_data WHERE record_id = %s", (checkin_id,)
    ).fetchone()
    table, key = "active_event_data", "record_id"
    if found is None:
        archived = conn.execute(
            "SELECT event_slug FROM event_archive WHERE source_record_id = %s", (checkin_id,)
        ).fetchall()
        if len(archived) != 1:
            return None
        found = archived[0]
        table, key = "event_archive", "source_record_id"
    slug = found[0]
    conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (slug,))
    conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
    # Table/key are constants selected above, never request-controlled identifiers.
    row = conn.execute(
        f"SELECT member, member_source, event_slug, tag FROM {table} "
        f"WHERE {key} = %s AND event_slug = %s FOR UPDATE",
        (checkin_id, slug),
    ).fetchone()
    return (
        dict(zip(("member", "member_source", "event_slug", "tag"), row), table=table)
        if row
        else None
    )


def _resolve_legacy_registration_by_to(conn, checkin_id, event_slug, member):
    """Called only after a TO member edit, in that edit's existing transaction."""
    changed = conn.execute(
        """UPDATE legacy_register_attempts
           SET state = 'resolved', resolved_by = 'to', updated_at = clock_timestamp()
           WHERE checkin_id = %s RETURNING checkin_id""",
        (checkin_id,),
    ).fetchone()
    if changed:
        _integration_audit(
            conn,
            "legacy_registration_resolved",
            "legacy_register_attempts",
            event_slug,
            checkin_id,
            {"resolved_by": "to", "member": member is True},
        )


def claim_legacy_registration(checkin_id: str, event_slug: str, can_submit: bool = True) -> dict:
    """Commit a durable, input-free tombstone before any external submission."""
    with _get_pool().connection() as conn, conn.transaction():
        owner = _lock_legacy_registration_owner(conn, checkin_id)
        if (
            owner is None
            or owner["table"] != "active_event_data"
            or owner["event_slug"] != event_slug
        ):
            return {"state": "missing"}
        attempt = conn.execute(
            "SELECT state, started_at, resolved_by FROM legacy_register_attempts "
            "WHERE checkin_id = %s FOR UPDATE",
            (checkin_id,),
        ).fetchone()
        if owner["member"] is True:
            if attempt and not (attempt[0] == "resolved" and attempt[2] == "to"):
                resolver = "to" if owner["member_source"] == "to" else "reconcile"
                conn.execute(
                    """UPDATE legacy_register_attempts SET state = 'accepted',
                       resolved_by = %s, updated_at = clock_timestamp() WHERE checkin_id = %s""",
                    (resolver, checkin_id),
                )
                if attempt[0] != "accepted" or attempt[2] != resolver:
                    _integration_audit(
                        conn,
                        "legacy_registration_reconciled",
                        "legacy_register_attempts",
                        event_slug,
                        checkin_id,
                        {"state": "accepted", "resolved_by": resolver},
                    )
            return {"state": "accepted", "member": True, "already_member": True}
        if attempt and (
            attempt[0] in ("in_flight", "unknown", "accepted")
            or (attempt[0] == "resolved" and attempt[2] != "to")
        ):
            return {"state": attempt[0], "started_at": attempt[1], "member": False}
        if not can_submit:
            return {"state": "invalid_input"}
        started_at = conn.execute(
            """INSERT INTO legacy_register_attempts (checkin_id, state)
               VALUES (%s, 'in_flight')
               ON CONFLICT (checkin_id) DO UPDATE SET state = 'in_flight',
                   started_at = clock_timestamp(), updated_at = clock_timestamp(), resolved_by = NULL
               RETURNING started_at""",
            (checkin_id,),
        ).fetchone()[0]
        _integration_audit(
            conn,
            "legacy_registration_started",
            "legacy_register_attempts",
            event_slug,
            checkin_id,
            {"state": "in_flight"},
        )
        return {"state": "claimed", "started_at": started_at, "tag": owner["tag"]}


def finish_legacy_registration(checkin_id: str, started_at, state: str) -> dict:
    """Never let an old completion erase a TO resolution or a newer reviewed retry."""
    if state not in ("accepted", "rejected", "unknown"):
        raise ValueError("Invalid legacy registration outcome")
    with _get_pool().connection() as conn, conn.transaction():
        owner = _lock_legacy_registration_owner(conn, checkin_id)
        if owner is None:
            return {"state": "unknown", "member": False}
        attempt = conn.execute(
            "SELECT state, started_at FROM legacy_register_attempts WHERE checkin_id = %s FOR UPDATE",
            (checkin_id,),
        ).fetchone()
        if not attempt or attempt[1] != started_at or attempt[0] not in ("in_flight", "unknown"):
            return {
                "state": attempt[0] if attempt else "unknown",
                "member": owner["member"] is True,
            }
        resolver = None
        if owner["member"] is True:
            state, resolver = "accepted", "reconcile"
        conn.execute(
            """UPDATE legacy_register_attempts SET state = %s, resolved_by = %s,
               updated_at = clock_timestamp() WHERE checkin_id = %s AND started_at = %s""",
            (state, resolver, checkin_id, started_at),
        )
        _integration_audit(
            conn,
            "legacy_registration_outcome",
            "legacy_register_attempts",
            owner["event_slug"],
            checkin_id,
            {"state": state},
        )
        return {"state": state, "member": owner["member"] is True}


def _membership_provenance(conn, current):
    """Protect known authority and pre-004 true values whose source cannot be reconstructed."""
    if current.get("member_source") is None and current.get("member") is True:
        return "to"
    if current.get("member_source") in ("to", "v2"):
        return current["member_source"]
    confirmed = conn.execute(
        """SELECT EXISTS (
               SELECT 1 FROM membership_attempts attempt
               JOIN membership_receipts receipt ON receipt.attempt_id = attempt.id
               WHERE attempt.checkin_id = %s AND attempt.event_slug = %s
                 AND attempt.state = 'submitted' AND receipt.outcome = 'submitted'
                 AND receipt.verified_at IS NOT NULL)""",
        (current["record_id"], current["event_slug"]),
    ).fetchone()[0]
    return "v2" if confirmed else current.get("member_source")


def validate_provider_verdict(source: str, payload: Any) -> Dict[str, Any]:
    """Validate a typed provider result without inferring truth from transport success."""

    def result(valid, state, error_code=None, data=None):
        return {
            "valid": valid,
            "ok": valid and state in {"verified_true", "verified_false"},
            "state": state,
            "error_code": error_code,
            "data": data if data is not None else {},
        }

    def reject(code):
        return result(False, "unavailable", code)

    if source not in {"ebas", "startgg"}:
        return reject("invalid_source")
    if not isinstance(payload, dict):
        return reject("invalid_payload")
    ok, state = payload.get("ok"), payload.get("state")
    if type(ok) is not bool:
        return reject("invalid_ok")
    if not isinstance(state, str) or state not in {
        "verified_true",
        "verified_false",
        "unavailable",
        "skipped",
    }:
        return reject("invalid_state")
    if state in {"unavailable", "skipped"}:
        if ok:
            return reject("contradictory_verdict")
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        safe_codes = {
            "api_unavailable",
            "provider_timeout",
            "timeout",
            "invalid_input",
            "provider_error",
        }
        safe_code = code if isinstance(code, str) and code in safe_codes else "provider_unavailable"
        return result(True, state, "skipped" if state == "skipped" else safe_code)
    if not ok:
        return reject("contradictory_verdict")

    # Inspect envelopes as well as data; never copy provider error values into an audit.
    pending = [payload]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if (key in {"error", "errors"} and value) or (
                    key == "request_result" and value != "success"
                ):
                    return reject("provider_error")
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)

    key = "member" if source == "ebas" else "registered"
    if "data" in payload:
        data = payload["data"]
        if not isinstance(data, dict):
            return reject("invalid_data")
    else:
        alias = "member" if source == "ebas" else "isRegistered"
        data = {key: payload[alias]} if alias in payload else {}
        if source == "startgg":
            for name in ("events", "email", "startgg_event_id"):
                if name in payload:
                    data[name] = payload[name]
    if key not in data:
        return reject("missing_verdict")
    if type(data[key]) is not bool:
        return reject("invalid_boolean")
    if data[key] is not (state == "verified_true"):
        return reject("contradictory_verdict")
    if (
        source == "startgg"
        and "events" in data
        and (
            not isinstance(data["events"], list)
            or any(not isinstance(event, str) or not event.strip() for event in data["events"])
        )
    ):
        return reject("invalid_events")
    return result(True, state, data=data)


def _safe_provider_attempt_id(attempt_id):
    """Only backend-shaped UUIDs are safe correlation metadata for the audit."""
    if not isinstance(attempt_id, str):
        return None
    try:
        return str(uuid.UUID(attempt_id))
    except ValueError:
        return None


def apply_legacy_member_status(
    checkin_id: str, member: Any, *, ok=None, state=None, error=None, attempt_id=None, envelope=None
):
    """Apply a legacy automated membership callback without erasing TO/v2 authority."""
    # TODO(T-053): Keep until PROD n8n runs the typed R flow (T-053) — remove in a later release, never before the flow import.
    compatibility = (
        isinstance(envelope, dict)
        and set(envelope) == {"member"}
        and envelope["member"] is True
        and member is True
        and ok is None
        and state is None
        and error is None
    )
    if compatibility:
        ok, state = True, "verified_true"
    verdict = validate_provider_verdict(
        "ebas",
        {
            "ok": ok,
            "state": state,
            "data": {"member": member},
            "error": error,
            "envelope": envelope,
        },
    )
    with _get_pool().connection() as conn, conn.transaction():
        current = _lock_integration_checkin(conn, checkin_id)
        if current is None:
            return None
        provenance = _membership_provenance(conn, current)
        fields = {}
        if verdict["ok"] and provenance != "to" and not (provenance == "v2" and not member):
            fields = {"member": member}
            if provenance != "v2":
                fields["member_source"] = "ebas_check"
        result = (
            _update_checkin_with_status(conn, checkin_id, fields, event_slug=current["event_slug"])
            if verdict["ok"]
            else {"fields": {"member": current["member"], "status": current["status"]}}
        )
        if verdict["ok"] and not result:
            raise RuntimeError("Failed to update checkin from legacy member result")
        if verdict["ok"] and result and result["fields"].get("member") is True:
            completed = conn.execute(
                """UPDATE legacy_register_attempts SET state = 'accepted',
                   resolved_by = 'reconcile', updated_at = clock_timestamp()
                   WHERE checkin_id = %s AND state IN ('in_flight', 'unknown')
                   RETURNING checkin_id""",
                (checkin_id,),
            ).fetchone()
            if completed:
                _integration_audit(
                    conn,
                    "legacy_registration_reconciled",
                    "legacy_register_attempts",
                    current["event_slug"],
                    checkin_id,
                    {"state": "accepted", "resolved_by": "reconcile"},
                )
        _integration_audit(
            conn,
            "legacy_member_result",
            "active_event_data",
            current["event_slug"],
            checkin_id,
            {
                "updated": bool(fields),
                "state": verdict["state"],
                "error_code": verdict["error_code"],
                "attempt_id": _safe_provider_attempt_id(attempt_id),
                "compatibility": "legacy_member_true" if compatibility else None,
            },
        )
        return {
            **(result or {}),
            "verdict_valid": verdict["valid"],
            "ok": verdict["ok"],
            "state": verdict["state"],
            "error_code": verdict["error_code"],
            "updated": bool(fields),
        }


def _acknowledge_membership_link(conn, attempt_id):
    conn.execute(
        """UPDATE membership_attempts SET member_link_reconciled_at = clock_timestamp(),
           last_reconciled_at = clock_timestamp(), updated_at = clock_timestamp()
           WHERE id = %s""",
        (attempt_id,),
    )


def mark_membership_checkin(attempt_id: str) -> str:
    """Link a submitted receipt to its active or exact archived check-in atomically."""
    from shared import membership_store as store

    attempt_id = store._id(attempt_id)
    with _get_pool().connection() as conn, conn.transaction():
        initial = conn.execute(
            "SELECT event_slug, checkin_id FROM membership_attempts WHERE id = %s", (attempt_id,)
        ).fetchone()
        if not initial:
            raise ValueError("Membership attempt is missing")
        event_slug = initial[0]
        conn.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext('fgt-event:' || %s))", (event_slug,)
        )
        conn.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
        checkin_id = initial[1]
        row = conn.execute(
            """SELECT member, member_source FROM active_event_data
               WHERE record_id = %s AND event_slug = %s FOR UPDATE""",
            (checkin_id, event_slug),
        ).fetchone()
        table = "active_event_data"
        if row is None:
            table = "event_archive"
            row = conn.execute(
                """SELECT member, member_source FROM event_archive
                   WHERE source_record_id = %s AND event_slug = %s FOR UPDATE""",
                (checkin_id, event_slug),
            ).fetchone()
        # Follow event -> merge -> check-in row -> membership-person/attempt order.
        attempt = store._locked_person_attempt(conn, attempt_id)
        if attempt["state"] != "submitted":
            return attempt["state"]
        if checkin_id is None:
            _acknowledge_membership_link(conn, attempt_id)
            return "submitted"
        if row is None:
            conn.execute(
                """UPDATE membership_attempts SET state = 'manual_review',
                   outcome_error_code = 'checkin_missing', updated_at = clock_timestamp()
                   WHERE id = %s""",
                (attempt_id,),
            )
            store._receipt_outcome(conn, attempt_id, "manual_review")
            _integration_audit(
                conn,
                "membership_checkin_missing",
                table,
                event_slug,
                checkin_id,
                {"outcome": "manual_review", "error_code": "checkin_missing"},
            )
            return "manual_review"
        # A TO override is authoritative even when a submitted receipt is polled again.
        if row[1] == "to" or (row[0] is True and row[1] == "v2"):
            _acknowledge_membership_link(conn, attempt_id)
            return "submitted"
        if table == "active_event_data":
            _update_checkin_with_status(
                conn, checkin_id, {"member": True, "member_source": "v2"}, event_slug=event_slug
            )
        else:
            conn.execute(
                """UPDATE event_archive SET member = true, member_source = 'v2'
                   WHERE source_record_id = %s AND event_slug = %s""",
                (checkin_id, event_slug),
            )
        _integration_audit(
            conn,
            "membership_member_updated",
            table,
            event_slug,
            checkin_id,
            {"member_source": "v2"},
        )
        _acknowledge_membership_link(conn, attempt_id)
    return "submitted"


def apply_integration_result(
    checkin_id: str,
    source: str,
    ok: bool,
    data: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
    fetched_at: Optional[str] = None,
    state: Optional[str] = None,
    attempt_id: Optional[str] = None,
    recompute_payment_expected: bool = False,
    envelope: Any = None,
) -> Dict[str, Any]:
    """Apply integration result to a checkin record and write audit log."""
    if not checkin_id:
        raise ValueError("checkin_id is required")
    if not isinstance(source, str) or source.strip().lower() not in {
        "ebas",
        "startgg",
        "swish",
        "stripe",
    }:
        raise ValueError("invalid_source")

    update_fields: Dict[str, Any] = {}

    src = source.lower().strip()
    if src in {"swish", "stripe"} and type(ok) is not bool:
        raise ValueError("invalid_ok")
    verdict = None
    if src in {"ebas", "startgg"}:
        verdict = validate_provider_verdict(
            src, {"ok": ok, "state": state, "data": data, "error": error, "envelope": envelope}
        )
        ok, state, data = verdict["ok"], verdict["state"], verdict["data"]
    else:
        data = data if isinstance(data, dict) else {}
    if src == "startgg":
        # Failed/skipped checks must preserve registration, games and contact data.
        if (
            ok
            and isinstance(data.get("registered"), bool)
            and state in ("verified_true", "verified_false")
        ):
            registered = data["registered"]
            update_fields["startgg"] = registered
            update_fields["is_guest"] = not registered
            if isinstance(data.get("events"), list):
                update_fields["tournament_games_registered"] = data.get("events")
            if data.get("startgg_event_id"):
                update_fields["startgg_event_id"] = str(data.get("startgg_event_id"))
            if data.get("email"):
                update_fields["email"] = data["email"]
    elif src == "ebas":
        member_lookup_state = "unknown"
        if state == "skipped":
            member_lookup_state = "skipped"
        elif (
            ok
            and isinstance(data.get("member"), bool)
            and state == ("verified_true" if data["member"] else "verified_false")
        ):
            update_fields["member"] = data["member"]
            member_lookup_state = "confirmed_member" if data["member"] else "confirmed_nonmember"
    elif src in ("swish", "stripe"):
        if data.get("payment_amount") is not None:
            update_fields["payment_amount"] = data.get("payment_amount")
        if data.get("payment_expected") is not None:
            update_fields["payment_expected"] = data.get("payment_expected")
        if data.get("payment_valid") is not None:
            update_fields["payment_valid"] = bool(data.get("payment_valid"))
        else:
            update_fields["payment_valid"] = bool(ok)

    audit_source = src if src in {"startgg", "ebas", "swish", "stripe"} else "unknown"
    result_time = None
    invalid_time = False
    if fetched_at is not None:
        try:
            result_time = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
            if result_time.tzinfo is None:
                raise ValueError
        except (TypeError, ValueError):
            result_time = None
            invalid_time = True
    with _get_pool().connection() as conn, conn.transaction():
        current = _lock_integration_checkin(conn, checkin_id)
        event_slug = current["event_slug"] if current else None
        stale = current is None
        if current and src in ("ebas", "startgg"):
            current_id = current[f"last_{src}_attempt_id"]
            stale = (attempt_id != current_id) if current_id else attempt_id is not None
            stale = stale or (current_id is not None and invalid_time)
            started = current[f"last_{src}_attempt_started_at"]
            previous = current[f"last_{src}_result_at"]
            if result_time is not None:
                stale = stale or (started is not None and result_time < started)
                stale = stale or (previous is not None and result_time <= previous)
            elif current_id and previous is not None:
                stale = True
        if stale:
            _integration_audit(
                conn,
                "stale_rejected",
                "active_event_data",
                event_slug,
                checkin_id,
                {
                    "source": audit_source,
                    "outcome": "stale_rejected",
                    "state": "stale_rejected",
                    "error_code": (
                        verdict["error_code"]
                        if verdict and not verdict["valid"]
                        else "stale_attempt"
                    ),
                    "attempt_id": _safe_provider_attempt_id(attempt_id),
                },
            )
            logger.info(
                "Integration result rejected: source=%s outcome=stale_rejected",
                audit_source,
            )
            return {
                "checkin_id": checkin_id,
                "source": src,
                "ok": ok,
                "updated": False,
                "state": "stale_rejected",
                "verdict_valid": verdict["valid"] if verdict else True,
                "error_code": verdict["error_code"] if verdict else None,
            }
        if src == "ebas" and "member" in update_fields:
            provenance = _membership_provenance(conn, current)
            if provenance == "to" or (provenance == "v2" and update_fields["member"] is False):
                update_fields.pop("member")
            elif provenance != "v2":
                update_fields["member_source"] = "ebas_check"
        if (
            recompute_payment_expected
            and src == "startgg"
            and "tournament_games_registered" in update_fields
        ):
            settings = conn.execute(
                "SELECT swish_expected_per_game FROM settings WHERE is_active=true ORDER BY id DESC LIMIT 1"
            ).fetchone()
            per_game = settings[0] if settings else 0
            if per_game:
                update_fields["payment_expected"] = (
                    len(update_fields["tournament_games_registered"]) * per_game
                )
        if update_fields and not _update_checkin_with_status(
            conn, checkin_id, update_fields, event_slug=event_slug
        ):
            raise RuntimeError("Failed to update checkin from integration result")
        if src in ("ebas", "startgg"):
            # src is a closed whitelist, never interpolated directly from arbitrary input.
            lookup_assignment = ", member_lookup_state = %s" if src == "ebas" else ""
            result_values = [result_time]
            if src == "ebas":
                result_values.append(member_lookup_state)
            result_values.append(checkin_id)
            conn.execute(
                f"UPDATE active_event_data SET last_{src}_result_at = COALESCE(%s, clock_timestamp())"
                f"{lookup_assignment} "
                "WHERE record_id = %s",
                result_values,
            )
        _integration_audit(
            conn,
            "integration_result",
            "active_event_data",
            event_slug,
            checkin_id,
            {
                "source": audit_source,
                "ok": ok is True,
                "updated_fields": sorted(update_fields),
                "state": state if verdict else "unavailable",
                "error_code": verdict["error_code"] if verdict else None,
                "attempt_id": _safe_provider_attempt_id(attempt_id),
            },
        )

    return {
        "checkin_id": checkin_id,
        "source": src,
        "ok": ok,
        "updated": bool(update_fields),
        "state": state if verdict else None,
        "error_code": verdict["error_code"] if verdict else None,
        "verdict_valid": verdict["valid"] if verdict else True,
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


def get_game_aliases() -> Dict[str, str]:
    """Read the exact, case-insensitive alias map without changing stored facts."""
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('game_aliases')")
        if cur.fetchone()[0] is None:
            return {}
        cur.execute("SELECT alias, canonical FROM game_aliases ORDER BY alias")
        return {
            str(alias).strip().lower(): str(canonical).strip()
            for alias, canonical in cur.fetchall()
        }


def _canonical_game_name(name, aliases):
    raw = str(name or "").strip()
    return aliases.get(raw.lower(), raw) if raw else ""


def _canonical_game_breakdown(breakdown, aliases):
    result = {}
    for game, count in breakdown.items() if isinstance(breakdown, dict) else []:
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            continue
        canonical = _canonical_game_name(game, aliases)
        if canonical and count > 0:
            result[canonical] = result.get(canonical, 0) + count
    return result


def get_event_game_stats(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    *,
    aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Aggregate distinct canonical games per archived check-in; never update rows."""
    aliases = get_game_aliases() if aliases is None else aliases
    conditions, params = [], []
    if event_slugs:
        conditions.append("event_slug = ANY(%s)")
        params.append(event_slugs)
    if start_date:
        conditions.append("event_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("event_date <= %s")
        params.append(end_date)
    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""
    with _get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_slug, tournament_games_registered FROM event_archive " + where_sql,
            params,
        )
        rows = cur.fetchall()
    result = {}
    for slug, raw_games in rows:
        stats = result.setdefault(
            slug,
            {
                "games_breakdown": {},
                "game_aliases": {},
                "total_count": 0,
                "multi_game_count": 0,
                "total_game_slots": 0,
                "unknown_game_count": 0,
                "metadata_complete": True,
            },
        )
        stats["total_count"] += 1
        if not isinstance(raw_games, (list, tuple)):
            stats["unknown_game_count"] += 1
            stats["metadata_complete"] = False
            continue
        games = set()
        for raw in raw_games:
            canonical = _canonical_game_name(raw, aliases)
            if not canonical:
                continue
            games.add(canonical)
            raw = str(raw).strip()
            if raw.lower() != canonical.lower():
                stats["game_aliases"].setdefault(canonical, set()).add(raw)
        stats["total_game_slots"] += len(games)
        stats["multi_game_count"] += len(games) >= 2
        for game in games:
            stats["games_breakdown"][game] = stats["games_breakdown"].get(game, 0) + 1
    for stats in result.values():
        stats["games_breakdown"] = dict(
            sorted(stats["games_breakdown"].items(), key=lambda item: item[0].lower())
        )
        stats["game_aliases"] = {
            canonical: sorted(labels, key=str.lower)
            for canonical, labels in stats["game_aliases"].items()
        }
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
                       startgg_snapshot,
                       no_show_count, no_show_rate
                FROM event_stats
                ORDER BY archived_at DESC NULLS LAST
                """
            )
            rows = cur.fetchall()

    aliases = get_game_aliases()
    game_stats = get_event_game_stats([row[0] for row in rows], aliases=aliases) if rows else {}
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
            startgg_snapshot,
            no_show_count,
            no_show_rate,
        ) = row

        canonical_breakdown = _canonical_game_breakdown(games_breakdown, aliases)
        archived_games = game_stats.get(event_slug)
        if archived_games:
            canonical_breakdown = archived_games["games_breakdown"]
        unknown_game_count = (
            archived_games["unknown_game_count"]
            + max(int(total_participants or 0) - archived_games["total_count"], 0)
            if archived_games
            else 0
        )
        alias_labels = {
            canonical: set(labels)
            for canonical, labels in (archived_games or {}).get("game_aliases", {}).items()
        }
        for raw in games_breakdown if isinstance(games_breakdown, dict) else {}:
            canonical = _canonical_game_name(raw, aliases)
            if canonical and str(raw).strip().lower() != canonical.lower():
                alias_labels.setdefault(canonical, set()).add(str(raw).strip())
        canonical_top = (
            min(canonical_breakdown, key=lambda game: (-canonical_breakdown[game], game.lower()))
            if canonical_breakdown
            else (
                (_canonical_game_name(most_popular_game, aliases) or None)
                if not archived_games
                else None
            )
        )
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
                "games_breakdown": canonical_breakdown,
                "unknown_game_count": unknown_game_count,
                "game_aliases": {
                    key: sorted(value, key=str.lower) for key, value in alias_labels.items()
                },
                "most_popular_game": canonical_top,
                "status_breakdown": status_breakdown,
                "startgg_registered_count": startgg_registered_count or 0,
                "checked_in_count": checked_in_count or 0,
                "startgg_snapshot": startgg_snapshot,
                "no_show_count": no_show_count or 0,
                "no_show_rate": float(no_show_rate or 0),
            }
        )

    # Preserve T065's canonical Games projection, then overlay T015's non-game facts.
    with _get_pool().connection() as conn, conn.transaction():
        context = _stats_context(conn)
        for row in result:
            slug = row["event_slug"]
            projected = (
                recompute_event_stats(slug, dry_run=True, _conn=conn, _context=context)["after"]
                if slug in context["events"]
                else context["metadata"].get(slug, {})
            )
            for key in (
                "event_date",
                "total_participants",
                "total_revenue",
                "avg_payment",
                "member_count",
                "member_percentage",
                "guest_count",
                "startgg_count",
                "status_breakdown",
                "new_players",
                "returning_players",
                "retention_rate",
                "same_series_return_rate",
                "same_series_base_count",
                "same_series_returned_players",
                "startgg_registered_count",
                "startgg_registered_players",
                "checked_in_count",
                "no_show_count",
                "no_show_rate",
                "requirements_snapshot",
            ):
                if key in projected:
                    row[key] = projected[key]
            value = row.get("event_date")
            row["event_date"] = value.isoformat() if hasattr(value, "isoformat") else value
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


def get_multi_game_count(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, int]:
    """Count check-ins with 2+ distinct canonical games in the selected scope."""
    stats = get_event_game_stats(event_slugs, start_date, end_date)
    return {
        "multi_game_count": sum(row["multi_game_count"] for row in stats.values()),
        "total_count": sum(row["total_count"] for row in stats.values()),
    }


def get_community_health_v2_stats(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    anchor_date: Optional[str] = None,
    recent_months: int = 6,
) -> Dict[str, float]:
    """Return Core Players (rolling window) and Player Lifetime stats."""

    scope_conditions: List[str] = []
    scope_params: List[Any] = []

    if event_slugs:
        scope_conditions.append("event_slug = ANY(%s)")
        scope_params.append(event_slugs)
    if start_date:
        scope_conditions.append("event_date >= %s")
        scope_params.append(start_date)
    if end_date:
        scope_conditions.append("event_date <= %s")
        scope_params.append(end_date)

    scope_where = f"WHERE {' AND '.join(scope_conditions)}" if scope_conditions else ""

    # Core players is always defined on a rolling recent window.
    anchor = anchor_date or end_date
    core_conditions: List[str] = ["ea.player_uuid IS NOT NULL"]
    core_params: List[Any] = []

    if event_slugs:
        core_conditions.append("ea.event_slug = ANY(%s)")
        core_params.append(event_slugs)

    if anchor:
        core_conditions.append("ea.event_date <= %s::date")
        core_params.append(anchor)
        core_conditions.append("ea.event_date >= (%s::date - (%s || ' months')::interval)")
        core_params.append(anchor)
        core_params.append(max(int(recent_months), 1))
    else:
        core_conditions.append("ea.event_date >= (CURRENT_DATE - (%s || ' months')::interval)")
        core_params.append(max(int(recent_months), 1))

    core_where = f"WHERE {' AND '.join(core_conditions)}"

    core_query = f"""
        SELECT COUNT(*)
        FROM (
            SELECT ea.player_uuid
            FROM event_archive ea
            {core_where}
            GROUP BY ea.player_uuid
            HAVING COUNT(DISTINCT ea.event_slug) >= 3
        ) core
    """

    if scope_conditions:
        lifetime_query = f"""
            WITH scoped_players AS (
                SELECT DISTINCT player_uuid
                FROM event_archive
                {scope_where}
                  AND player_uuid IS NOT NULL
            )
            SELECT AVG(p.total_events)::float
            FROM players p
            JOIN scoped_players sp ON sp.player_uuid = p.uuid
            WHERE p.total_events > 0
        """
    else:
        lifetime_query = """
            SELECT AVG(total_events)::float
            FROM players
            WHERE total_events > 0
        """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(core_query, core_params)
            core_row = cur.fetchone()

            cur.execute(lifetime_query, scope_params)
            lifetime_row = cur.fetchone()

    return {
        "core_players": int((core_row[0] if core_row else 0) or 0),
        "player_lifetime": float((lifetime_row[0] if lifetime_row else 0.0) or 0.0),
    }


def get_player_funnel_stats(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    anchor_date: Optional[str] = None,
    recent_months: int = 6,
    churn_months: int = 8,
) -> Dict[str, int]:
    """Unique UUID cohorts using the same first-attendance rule as event/KPI counts.

    A person can be new at one selected event and returning at another. Funnel cohorts
    therefore overlap; returning is unique people here versus event visits in the KPI.
    """

    def as_day(value):
        return datetime.fromisoformat(str(value)).date() if value else None

    lower, upper = as_day(start_date), as_day(end_date)
    new, returning, scoped = set(), set(), set()
    with _get_pool().connection() as conn, conn.transaction():
        context = _stats_context(conn)
        for slug, rows in context["events"].items():
            day = context["order"][slug][0]
            if day is None or (event_slugs and slug not in event_slugs):
                continue
            if (lower and day < lower) or (upper and day > upper):
                continue
            for row in rows:
                player = row["player_uuid"]
                if player:
                    scoped.add(player)
                    (new if context["first"].get(player) == slug else returning).add(player)
        # Preserve Core and Churn's existing time-window definitions.
        row = conn.execute(
            """SELECT
                (SELECT count(*) FROM (
                    SELECT player_uuid FROM event_archive
                    WHERE player_uuid = ANY(%s) AND event_date <= coalesce(%s::date, CURRENT_DATE)
                      AND event_date >= coalesce(%s::date, CURRENT_DATE) - (%s || ' months')::interval
                      AND (%s::text[] IS NULL OR event_slug = ANY(%s))
                    GROUP BY player_uuid HAVING count(DISTINCT event_slug) >= 3
                ) core),
                (SELECT count(*) FROM players WHERE total_events > 0
                 AND last_seen::date < CURRENT_DATE - (%s || ' months')::interval)""",
            (
                sorted(scoped),
                anchor_date or end_date,
                anchor_date or end_date,
                max(int(recent_months), 1),
                event_slugs or None,
                event_slugs or None,
                max(int(churn_months), 1),
            ),
        ).fetchone()
    return {
        "new_count": len(new),
        "returning_count": len(returning),
        "core_count": int(row[0] or 0),
        "churned_count": int(row[1] or 0),
    }


def get_game_crossover_stats(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Return game-pair crossover counts (distinct players per pair)."""

    conditions: List[str] = [
        "ea.player_uuid IS NOT NULL",
        "NULLIF(BTRIM(game.value), '') IS NOT NULL",
    ]
    params: List[Any] = []

    if event_slugs:
        conditions.append("ea.event_slug = ANY(%s)")
        params.append(event_slugs)
    if start_date:
        conditions.append("ea.event_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("ea.event_date <= %s")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(conditions)}"

    aliases = get_game_aliases()
    query = f"""
        WITH player_games AS (
            SELECT DISTINCT
                ea.player_uuid,
                COALESCE((%s::jsonb)->>LOWER(BTRIM(game.value)), BTRIM(game.value)) AS game
            FROM event_archive ea
            JOIN LATERAL unnest(
                COALESCE(ea.tournament_games_registered, ARRAY[]::text[])
            ) AS game(value) ON TRUE
            {where_sql}
        )
        SELECT
            pg1.game AS game_a,
            pg2.game AS game_b,
            COUNT(DISTINCT pg1.player_uuid) AS shared_players
        FROM player_games pg1
        JOIN player_games pg2
          ON pg1.player_uuid = pg2.player_uuid
         AND pg1.game < pg2.game
        GROUP BY pg1.game, pg2.game
        ORDER BY shared_players DESC, game_a ASC, game_b ASC
        LIMIT %s
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, [json.dumps(aliases), *params, max(int(limit), 1)])
            rows = cur.fetchall()

    result: List[Dict[str, Any]] = []
    for game_a, game_b, shared_players in rows:
        result.append(
            {
                "game_a": str(game_a or ""),
                "game_b": str(game_b or ""),
                "shared_players": int(shared_players or 0),
            }
        )
    return result


def get_acquisition_source_breakdown(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return acquisition_source distribution for archived rows in scope."""
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
            CASE
                WHEN acquisition_source IS NULL OR btrim(acquisition_source) = '' THEN 'unknown'
                WHEN LOWER(btrim(acquisition_source)) IN ('start.gg', 'startgg') THEN 'startgg'
                WHEN LOWER(btrim(acquisition_source)) IN ('social media', 'social_media', 'social') THEN 'social'
                WHEN LOWER(btrim(acquisition_source)) IN (
                    'friend', 'discord', 'venue', 'other', 'unknown'
                ) THEN LOWER(btrim(acquisition_source))
                ELSE 'other'
            END AS source,
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


def get_player_churn_stats(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    anchor_date: Optional[str] = None,
    churn_months: int = 3,
) -> Dict[str, float]:
    """Return churn count/rate for players in selected scope."""

    conditions: List[str] = ["ea.player_uuid IS NOT NULL"]
    params: List[Any] = []

    if event_slugs:
        conditions.append("ea.event_slug = ANY(%s)")
        params.append(event_slugs)
    if start_date:
        conditions.append("ea.event_date >= %s")
        params.append(start_date)
    if end_date:
        conditions.append("ea.event_date <= %s")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(conditions)}"
    anchor = anchor_date or end_date
    months = max(int(churn_months), 1)

    if anchor:
        churn_expr = "p.last_seen::date < (%s::date - (%s || ' months')::interval)"
        churn_params: List[Any] = [anchor, months]
    else:
        churn_expr = "p.last_seen::date < (CURRENT_DATE - (%s || ' months')::interval)"
        churn_params = [months]

    query = f"""
        WITH scoped_players AS (
            SELECT DISTINCT ea.player_uuid
            FROM event_archive ea
            {where_sql}
        )
        SELECT
            COUNT(*) FILTER (WHERE p.total_events > 0 AND {churn_expr}) AS churn_count,
            COUNT(*) AS scoped_players
        FROM scoped_players sp
        JOIN players p ON p.uuid = sp.player_uuid
    """

    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params + churn_params)
            row = cur.fetchone()

    if not row:
        return {"churn_count": 0, "scoped_players": 0, "churn_rate": 0.0}

    churn_count = int(row[0] or 0)
    scoped_players = int(row[1] or 0)
    churn_rate = (churn_count / scoped_players * 100.0) if scoped_players > 0 else 0.0

    return {
        "churn_count": churn_count,
        "scoped_players": scoped_players,
        "churn_rate": churn_rate,
    }


def get_top_players_history(
    event_slugs: Optional[List[str]] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 15,
    game_filter: Optional[str] = None,
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

    selected_game = str(game_filter or "").strip()
    if selected_game:
        aliases = get_game_aliases()
        canonical = _canonical_game_name(selected_game, aliases)
        keys = {key for key, value in aliases.items() if value.lower() == canonical.lower()}
        keys.add(canonical.lower())
        conditions.append(
            """
            EXISTS (
                SELECT 1
                FROM unnest(COALESCE(tournament_games_registered, ARRAY[]::TEXT[])) AS g(game_name)
                WHERE LOWER(BTRIM(g.game_name)) = ANY(%s)
            )
        """
        )
        params.append(sorted(keys))

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
    total_revenue = sum(Decimal(str(c.get("payment_amount") or 0)) for c in checkins)
    avg_payment = (total_revenue / total).quantize(Decimal("0.01"))

    # Segments
    member_count = sum(1 for c in checkins if c.get("member"))
    guest_count = sum(1 for c in checkins if c.get("is_guest"))
    startgg_count = sum(1 for c in checkins if c.get("startgg"))
    member_pct = (Decimal(member_count) / Decimal(total) * 100).quantize(Decimal("0.01"))

    # Games breakdown
    games: Dict[str, int] = {}
    for c in checkins:
        for g in c.get("tournament_games_registered") or []:
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


def _build_event_stats_integrity_warnings(
    *,
    total_participants: int,
    new_players: int,
    returning_players: int,
    checked_in_count: int,
    no_show_base: int,
    no_show_count: int,
    no_show_rate: Decimal,
    games_breakdown: Dict[str, int],
    payment_valid_count: int,
    paid_amount_count: int,
) -> List[str]:
    warnings: List[str] = []

    if (new_players + returning_players) != total_participants:
        warnings.append(
            f"funnel_mismatch:new+returning={new_players + returning_players},participants={total_participants}"
        )

    if checked_in_count > total_participants:
        warnings.append(
            f"checkedin_gt_participants:checked_in={checked_in_count},participants={total_participants}"
        )

    expected_no_show = max(no_show_base - checked_in_count, 0)
    if no_show_count != expected_no_show:
        warnings.append(
            f"noshow_mismatch:no_show={no_show_count},expected={expected_no_show},base={no_show_base},checked_in={checked_in_count}"
        )

    if no_show_rate < 0 or no_show_rate > 100:
        warnings.append(f"noshow_rate_out_of_bounds:{no_show_rate}")

    total_game_entries = sum(int(v or 0) for v in (games_breakdown or {}).values())
    if total_game_entries < total_participants:
        warnings.append(
            f"games_breakdown_low:games_total={total_game_entries},participants={total_participants}"
        )

    if payment_valid_count > paid_amount_count:
        warnings.append(
            f"payment_gap:payment_valid={payment_valid_count},payment_amount_gt_zero={paid_amount_count}"
        )

    return warnings


def _stats_context(
    conn, *, free_events=(), event_prices=None, membership_requirements=None, member_not_recorded=()
):
    """Read archive facts once; optional verified corrections remain an in-memory preview."""
    with conn.cursor() as cur:
        # Dry runs also support pre-010 schemas; do not cache a schema-dependent row shape.
        cur.execute("SELECT * FROM event_stats ORDER BY event_slug", prepare=False)
        columns = [item[0] for item in cur.description]
        metadata = {
            row["event_slug"]: row
            for row in (_row_to_dict(columns, value) for value in cur.fetchall())
        }
        cur.execute(
            "SELECT id, event_slug, event_date, event_display_name, archived_at, player_uuid, "
            "member, startgg, payment_valid, payment_amount, payment_expected, "
            "tournament_games_registered, status, is_guest FROM event_archive ORDER BY event_slug, id"
        )
        columns = [item[0] for item in cur.description]
        rows = [_row_to_dict(columns, value) for value in cur.fetchall()]
        # Consume T065's exact alias map through this existing read-only connection.
        cur.execute("SELECT to_regclass('game_aliases')")
        aliases = {}
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT alias, canonical FROM game_aliases ORDER BY alias")
            aliases = {
                str(alias).strip().lower(): str(canonical).strip()
                for alias, canonical in cur.fetchall()
            }
    events, corrections, price_warnings = {}, [], []
    attendance_counts = {}
    for row in rows:
        if row["player_uuid"]:
            pair = (row["event_slug"], row["player_uuid"])
            attendance_counts[pair] = attendance_counts.get(pair, 0) + 1
    for row in rows:
        slug = row["event_slug"]
        updates = {}
        if slug in free_events:
            if row["payment_expected"] != Decimal("0"):
                updates["payment_expected"] = Decimal("0")
        elif (
            slug in (event_prices or {})
            and attendance_counts.get((slug, row["player_uuid"]), 0) > 1
        ):
            # Two rows cannot establish two payment obligations for one attendance.
            price_warnings.append(
                {
                    "kind": "duplicate_payment_requires_review",
                    "event_slug": slug,
                    "archive_id": row["id"],
                }
            )
        elif slug in (event_prices or {}):
            amount, per_game = event_prices[slug]
            games = {
                _canonical_game_name(game, aliases)
                for game in row["tournament_games_registered"] or []
            }
            games.discard("")
            if per_game and not games:
                price_warnings.append(
                    {
                        "kind": "price_game_count_unknown",
                        "event_slug": slug,
                        "archive_id": row["id"],
                    }
                )
            else:
                expected = amount * (len(games) if per_game else 1)
                if row["payment_expected"] != expected:
                    updates["payment_expected"] = expected
                # A tariff establishes the expectation, never the recorded amount.
        if updates:
            corrections.append(
                {
                    "archive_id": row["id"],
                    "event_slug": slug,
                    "before": {key: row[key] for key in updates},
                    "after": updates,
                }
            )
            row.update(updates)
        events.setdefault(slug, []).append(row)
    order = {}
    for slug, attendance in events.items():
        dates = {row["event_date"] for row in attendance if row["event_date"] is not None}
        meta = metadata.get(slug, {})
        day = (
            next(iter(dates)) if len(dates) == 1 else meta.get("event_date") if not dates else None
        )
        stamps = [row["archived_at"] for row in attendance if row["archived_at"] is not None]
        if meta.get("archived_at") is not None:
            stamps.append(meta["archived_at"])
        stamp = min(stamps) if stamps else None
        order[slug] = (day, stamp)

    def key(slug):
        day, stamp = order[slug]
        return (
            day is None,
            day or datetime.max.date(),
            stamp or datetime.max.replace(tzinfo=timezone.utc),
            slug,
        )

    ordered = sorted(events, key=key)
    first, previous, last_by_series = {}, {}, {}
    for slug in ordered:
        if order[slug][0] is None:
            continue
        series = (
            "fightbox"
            if slug.lower().startswith("fightbox")
            else "fight-night" if slug.lower().startswith("fight-night") else None
        )
        if series:
            previous[slug] = last_by_series.get(series)
            last_by_series[series] = slug
        for row in events[slug]:
            if row["player_uuid"]:
                first.setdefault(row["player_uuid"], slug)
    return {
        "metadata": metadata,
        "events": events,
        "order": order,
        "ordered": ordered,
        "first": first,
        "previous_same_series": previous,
        "free_events": set(free_events),
        "event_prices": event_prices or {},
        "membership_requirements": membership_requirements or {},
        "member_not_recorded": set(member_not_recorded),
        "corrections": corrections,
        "price_warnings": price_warnings,
        "game_aliases": aliases,
    }


def _stats_audit(conn, action, table, details, *, user=None, event_slug=None):
    """Write the repair audit in the same transaction; failure must roll back the repair."""
    actor = user or {"user_name": "system"}
    conn.execute(
        "INSERT INTO audit_log (user_id, user_name, user_email, action, target_table, target_event, details) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            actor.get("user_id"),
            actor.get("user_name", "system"),
            actor.get("user_email"),
            action,
            table,
            event_slug,
            json.dumps(details, default=str),
        ),
    )


def recompute_event_stats(
    event_slug: str,
    *,
    user: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    _conn=None,
    _audit: bool = True,
    _context=None,
) -> Dict[str, Any]:
    """Canonical archive derivation; unknown dates never become today's date.

    First attendance is ordered by event date, first archived_at, then slug. Each UUID
    is new once, globally across all series. retention_rate retains its historic
    returning/participants meaning, displayed as Familiar faces. The separate
    same_series_return_rate measures the previous dated event's UUID cohort within
    fightbox/fight-night. First events and collaborations have no series baseline.
    A supplied context
    lets a SELECT-only CLI preview verified price/free inputs without changing facts.
    """
    from contextlib import nullcontext
    from psycopg.types.json import Json

    if not event_slug:
        raise ValueError("event_slug is required")
    resource = nullcontext(_conn) if _conn is not None else _get_pool().connection()
    with resource as conn, conn.transaction():
        context = _context or _stats_context(conn)
        checkins = context["events"].get(event_slug)
        if not checkins:
            raise ValueError("No archive attendance facts for selected event")
        previous = context["metadata"].get(event_slug, {})
        event_date, archived_at = context["order"][event_slug]
        players = {row["player_uuid"] for row in checkins if row["player_uuid"]}
        new_ids = sorted(player for player in players if context["first"].get(player) == event_slug)
        returning = len(players) - len(new_ids) if event_date is not None else 0
        stats = compute_event_stats(checkins)
        # Archive rows remain immutable facts. Known identities establish one visit;
        # unlinked rows remain separate, flagged attendances until a human links them.
        visits = {}
        for row in checkins:
            key = ("player", row["player_uuid"]) if row["player_uuid"] else ("unlinked", row["id"])
            visits.setdefault(key, []).append(row)
        stats["total_participants"] = len(visits)
        stats["avg_payment"] = (stats["total_revenue"] / len(visits)).quantize(Decimal("0.01"))
        for field, flag in (
            ("member_count", "member"),
            ("guest_count", "is_guest"),
            ("startgg_count", "startgg"),
        ):
            stats[field] = sum(any(row[flag] for row in visit) for visit in visits.values())
        stats["member_percentage"] = (Decimal(stats["member_count"]) / len(visits) * 100).quantize(
            Decimal("0.01")
        )
        statuses = {}
        for visit in visits.values():
            observed = {row["status"] or "Unknown" for row in visit}
            status = (
                "Ready"
                if "Ready" in observed
                else next(iter(observed)) if len(observed) == 1 else "Conflicting"
            )
            statuses[status] = statuses.get(status, 0) + 1
        stats["status_breakdown"] = statuses
        # Stored Games summaries stay on the existing path, owned by T065.
        requirements = dict(previous.get("requirements_snapshot") or {})
        if event_slug in context["free_events"]:
            requirements["require_payment"] = False
            requirements.pop("event_price", None)
        if event_slug in context["event_prices"]:
            amount, per_game = context["event_prices"][event_slug]
            requirements["require_payment"] = True
            requirements["event_price"] = {
                "amount": str(amount),
                "basis": "game" if per_game else "person",
            }
        if event_slug in context["membership_requirements"]:
            requirements["require_membership"] = context["membership_requirements"][event_slug]
        if event_slug in context["member_not_recorded"]:
            requirements["member_data_recorded"] = False
        stats["member_percentage"] = (
            stats["member_percentage"]
            if requirements.get("require_membership") is True
            and requirements.get("member_data_recorded") is not False
            else None
        )
        prior_slug = context["previous_same_series"].get(event_slug)
        prior = (
            {row["player_uuid"] for row in context["events"][prior_slug] if row["player_uuid"]}
            if prior_slug
            else set()
        )
        retained = len(players & prior) if prior else None
        baseline = len(prior) if prior else None
        series_return = (
            (Decimal(retained) / baseline * 100).quantize(Decimal("0.01")) if baseline else None
        )
        retention = (
            (Decimal(returning) / len(visits) * 100).quantize(Decimal("0.01"))
            if event_date is not None
            else None
        )
        snapshot = previous.get("startgg_snapshot")
        registered_slots, registered_players = 0, None
        if isinstance(snapshot, dict):
            registered_slots = int(snapshot.get("tournament_entrants") or 0)
            if snapshot.get("tournament_entrants_players") is not None:
                registered_players = int(snapshot["tournament_entrants_players"])
        elif isinstance(snapshot, list):
            registered_slots = sum(
                int(item.get("numEntrants") or 0) for item in snapshot if isinstance(item, dict)
            )
        total = stats["total_participants"]
        # Slots are not unique registrations; a signup is not proof of attendance.
        no_show = max(registered_players - total, 0) if registered_players is not None else None
        no_show_rate = (
            (Decimal(no_show) / registered_players * 100).quantize(Decimal("0.01"))
            if registered_players
            else Decimal("0") if registered_players == 0 else None
        )
        after = {
            "event_date": event_date,
            "event_display_name": previous.get("event_display_name")
            or next(
                (row["event_display_name"] for row in checkins if row["event_display_name"]),
                event_slug,
            ),
            "archived_at": archived_at,
            **stats,
            "new_players": len(new_ids),
            "returning_players": returning,
            "retention_rate": retention,
            "same_series_return_rate": series_return,
            "same_series_base_count": baseline,
            "same_series_returned_players": retained,
            "requirements_snapshot": requirements or None,
            "startgg_registered_count": registered_slots,
            "startgg_registered_players": registered_players,
            "checked_in_count": total,
            "no_show_count": no_show,
            "no_show_rate": no_show_rate,
        }
        before = {key: previous.get(key) for key in after} if previous else None
        changes = {
            key: {"before": (before or {}).get(key), "after": value}
            for key, value in after.items()
            if before is None or before.get(key) != value
        }
        warnings = []
        if event_date is None:
            warnings.append("unknown_or_conflicting_event_date")
        if len(new_ids) + returning != total:
            warnings.append("new_returning_participant_mismatch")
        if registered_players is None:
            warnings.append("unique_registration_count_unknown")
        if requirements.get("require_payment") is not False and any(
            row["payment_valid"] and not row["payment_amount"] for row in checkins
        ):
            warnings.append("payment_valid_without_amount")
        if not dry_run and changes:
            with conn.cursor() as cur:
                values = [
                    (
                        Json(value)
                        if key in {"games_breakdown", "status_breakdown", "requirements_snapshot"}
                        and value is not None
                        else value
                    )
                    for key, value in after.items()
                ]
                if previous:
                    assignments = ", ".join(f"{key} = %s" for key in after)
                    cur.execute(
                        f"UPDATE event_stats SET {assignments} WHERE event_slug = %s",
                        [*values, event_slug],
                    )
                else:
                    names = ", ".join(["event_slug", *after])
                    placeholders = ", ".join(["%s"] * (len(values) + 1))
                    cur.execute(
                        f"INSERT INTO event_stats ({names}) VALUES ({placeholders})",
                        [event_slug, *values],
                    )
        if not dry_run and _audit:
            _stats_audit(
                conn,
                "event_stats_recomputed",
                "event_stats",
                {"changed_fields": sorted(changes)},
                user=user,
                event_slug=event_slug,
            )
    return {
        "event_slug": event_slug,
        "participants": total,
        "archive_rows": len(checkins),
        **after,
        "total_revenue": float(stats["total_revenue"]),
        "new_player_uuids": new_ids,
        "before": before,
        "after": after,
        "changes": changes,
        "integrity_warnings": warnings,
    }


def recompute_player_aggregates(
    dry_run: bool = True, *, _conn=None, _audit: bool = True, _context=None
):
    """Count distinct event visits and sum all recorded payments; never change identity facts."""
    from contextlib import nullcontext
    from psycopg.types.json import Json

    resource = nullcontext(_conn) if _conn is not None else _get_pool().connection()
    differences = []
    with resource as conn, conn.transaction():
        context = _context or _stats_context(conn)
        with conn.cursor() as cur:
            fields = [
                "total_events",
                "total_paid",
                "first_seen",
                "last_seen",
                "first_event",
                "last_event",
                "events_list",
                "games_played",
                "game_counts",
                "favorite_game",
            ]
            cur.execute("SELECT uuid, " + ", ".join(fields) + " FROM players ORDER BY uuid")
            columns = [item[0] for item in cur.description]
            profiles = [_row_to_dict(columns, row) for row in cur.fetchall()]
            attendance = {}
            for slug, rows in context["events"].items():
                for row in rows:
                    attendance.setdefault(row["player_uuid"], []).append(row)
            for profile in profiles:
                rows = attendance.get(profile["uuid"], [])
                attended = {row["event_slug"] for row in rows}
                events = [slug for slug in context["ordered"] if slug in attended]
                dated = [slug for slug in events if context["order"][slug][0] is not None]

                def seen(slug):
                    value = context["order"][slug][0]
                    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

                games = {}
                for row in rows:
                    for game in {
                        _canonical_game_name(raw, context["game_aliases"])
                        for raw in row["tournament_games_registered"] or []
                    } - {""}:
                        games[game] = games.get(game, 0) + 1
                games = dict(sorted(games.items()))
                after = {
                    "total_events": len(events),
                    "total_paid": sum(
                        (row["payment_amount"] or Decimal("0") for row in rows), Decimal("0")
                    ),
                    "first_seen": seen(dated[0]) if dated else None,
                    "last_seen": seen(dated[-1]) if dated else None,
                    "first_event": dated[0] if dated else None,
                    "last_event": dated[-1] if dated else None,
                    "events_list": events,
                    "games_played": list(games),
                    "game_counts": games,
                    "favorite_game": (
                        min(games, key=lambda game: (-games[game], game)) if games else None
                    ),
                }
                before = {key: profile[key] for key in after}
                changes = {
                    key: {"before": before[key], "after": value}
                    for key, value in after.items()
                    if before[key] != value
                }
                if changes:
                    differences.append(
                        {
                            "player_uuid": profile["uuid"],
                            "before": before,
                            "after": after,
                            "changes": changes,
                        }
                    )
                    if not dry_run:
                        values = [
                            Json(value) if key in {"events_list", "game_counts"} else value
                            for key, value in after.items()
                        ]
                        assignments = ", ".join(f"{key} = %s" for key in after)
                        cur.execute(
                            f"UPDATE players SET {assignments} WHERE uuid = %s",
                            [*values, profile["uuid"]],
                        )
        if not dry_run and _audit:
            _stats_audit(
                conn,
                "player_aggregates_recomputed",
                "players",
                {"changed_players": len(differences)},
            )
    return differences


def scan_event_stats_integrity(
    event_slugs: Optional[List[str]] = None, *, _conn=None, _context=None
):
    """Separate repairable derived values from payment, attendance and identity deviations."""
    from contextlib import nullcontext

    data_errors, deviations = [], []
    resource = nullcontext(_conn) if _conn is not None else _get_pool().connection()
    with resource as conn, conn.transaction():
        context = _context or _stats_context(conn)
        slugs = sorted(set(context["events"]) | set(context["metadata"]))
        selected = set(event_slugs) if event_slugs else set(slugs)
        selected_players = set()
        for slug in slugs:
            if slug not in selected:
                continue
            rows = context["events"].get(slug, [])
            if not rows:
                deviations.append({"kind": "stats_without_attendance", "event_slug": slug})
                continue
            result = recompute_event_stats(slug, dry_run=True, _conn=conn, _context=context)
            if result["changes"]:
                data_errors.append(
                    {
                        "kind": "event_stats_mismatch",
                        "event_slug": slug,
                        "changes": result["changes"],
                    }
                )
            requirements = result["after"]["requirements_snapshot"] or {}
            for warning in result["integrity_warnings"]:
                if warning != "payment_valid_without_amount":
                    deviations.append({"kind": warning, "event_slug": slug})
            if requirements.get("require_membership") is None:
                deviations.append({"kind": "membership_requirement_unknown", "event_slug": slug})
            if requirements.get("member_data_recorded") is False:
                deviations.append({"kind": "member_data_not_recorded", "event_slug": slug})
            if (
                requirements.get("require_membership") is True
                and result["after"]["member_count"] == 0
            ):
                deviations.append({"kind": "required_membership_zero_members", "event_slug": slug})
            if requirements.get("require_payment") is None:
                deviations.append(
                    {"kind": "payment_requirement_or_price_unknown", "event_slug": slug}
                )
            counts = {}
            for row in rows:
                player = row["player_uuid"]
                selected_players.add(player)
                base = {"event_slug": slug, "archive_id": row["id"]}
                if not player:
                    deviations.append({"kind": "missing_player_uuid", **base})
                else:
                    counts[player] = counts.get(player, 0) + 1
                amount, expected = row["payment_amount"] or Decimal("0"), row[
                    "payment_expected"
                ] or Decimal("0")
                if (
                    row["payment_valid"]
                    and amount == 0
                    and requirements.get("require_payment") is not False
                ):
                    deviations.append(
                        {
                            "kind": "payment_valid_without_amount",
                            "message": "Paid, amount not recorded",
                            **base,
                        }
                    )
                if row["payment_valid"] and amount < expected:
                    deviations.append(
                        {
                            "kind": "paid_below_expected",
                            **base,
                            "amount": amount,
                            "expected": expected,
                        }
                    )
            for player, count in counts.items():
                if count > 1:
                    deviations.append(
                        {
                            "kind": "duplicate_event_attendance",
                            "event_slug": slug,
                            "player_uuid": player,
                            "rows": count,
                        }
                    )
        deviations.extend(
            item for item in context["price_warnings"] if item["event_slug"] in selected
        )
        for diff in recompute_player_aggregates(dry_run=True, _conn=conn, _context=context):
            if event_slugs and diff["player_uuid"] not in selected_players:
                continue
            data_errors.append({"kind": "player_aggregates_mismatch", **diff})
            if "total_paid" in diff["changes"]:
                deviations.append(
                    {
                        "kind": "player_payment_aggregate_mismatch",
                        "player_uuid": diff["player_uuid"],
                        "before": diff["before"]["total_paid"],
                        "after": diff["after"]["total_paid"],
                    }
                )
            if "total_events" in diff["changes"]:
                deviations.append(
                    {"kind": "player_total_events_mismatch", "player_uuid": diff["player_uuid"]}
                )
        profiles = conn.execute("SELECT uuid, name, tag FROM players ORDER BY uuid").fetchall()
        identities = {}
        for player, name, tag in profiles:
            for field, value in (("name", name), ("tag", tag)):
                if value and str(value).strip():
                    identities.setdefault((field, str(value).strip().casefold()), set()).add(player)
        for (field, value), players in sorted(identities.items()):
            if len(players) > 1 and (not event_slugs or players & selected_players):
                deviations.append(
                    {
                        "kind": "suspected_duplicate",
                        "field": field,
                        "value": value,
                        "player_uuids": sorted(players),
                    }
                )
        known = {profile[0] for profile in profiles}
        for slug, rows in context["events"].items():
            if slug in selected:
                for row in rows:
                    if row["player_uuid"] and row["player_uuid"] not in known:
                        deviations.append(
                            {
                                "kind": "missing_player_profile",
                                "event_slug": slug,
                                "archive_id": row["id"],
                            }
                        )
    return {"data_errors": data_errors, "deviations": deviations}


def _find_player_uuid(tag: Optional[str], email: Optional[str], cur=None) -> Optional[str]:
    """
    Lightweight player lookup — match only, no creation or stat updates.

    Used during active check-in (begin_checkin) to link a check-in to an
    existing player profile.  Returns the player UUID if found, else None.

    Match priority: tag (case-insensitive) > email (case-insensitive).
    Reuse a supplied cursor when the caller already owns a pool connection.
    """
    if not tag and not email:
        return None

    if cur is None:
        with _get_pool().connection() as conn, conn.cursor() as lookup_cursor:
            return _find_player_uuid(tag, email, cur=lookup_cursor)

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

    def _to_date(value: Any):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
            try:
                # datetime.date without importing date directly
                return value
            except Exception:
                return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except Exception:
            return None

    event_date_obj = _to_date(event_date)

    # Try match by tag, then email
    player_row = None
    select_cols = """
        uuid, games_played, total_events, total_paid,
        events_list, game_counts, first_seen, last_seen, last_event
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
            p_uuid,
            p_games,
            p_total_events,
            p_total_paid,
            p_events_list,
            p_game_counts,
            p_first_seen,
            p_last_seen,
            p_last_event,
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

        p_last_seen_date = p_last_seen
        new_last_seen = p_last_seen_date
        new_last_event = p_last_event
        if is_new_event:
            if isinstance(p_last_seen_date, datetime):
                p_last_seen_date = p_last_seen_date.date()
            if (p_last_seen_date is None) or (
                event_date_obj is not None and event_date_obj >= p_last_seen_date
            ):
                new_last_seen = event_date_obj or event_date
                new_last_event = event_slug

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
                tag,
                email,
                checkin.get("name"),
                checkin.get("telephone"),
                updated_games,
                _Json(p_game_counts),
                favorite,
                new_total_events,
                payment_to_add,
                new_last_seen,
                new_last_event,
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
                checkin.get("name"),
                tag,
                email,
                checkin.get("telephone"),
                new_games or [],
                _Json(game_counts) if game_counts else _Json({}),
                favorite,
                1,
                payment,
                event_date,
                event_date,
                event_slug,
                event_slug,
                _Json([event_slug]),
                checkin.get("member") or False,
                now,
                now,
            ),
        )
        new_uuid = cur.fetchone()[0]
        return (new_uuid, True)


class ArchiveError(ValueError):
    """Archiving cannot complete without discarding or guessing event data."""


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
        not event_date
        or not event_display_name
        or not swish_expected_per_game
        or startgg_snapshot is None
    )
    if needs_settings:
        settings = get_active_settings()
    else:
        settings = None

    existing_stats: Optional[Dict[str, Any]] = None
    if not event_date or not event_display_name or startgg_snapshot is None:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_date, event_display_name, startgg_snapshot
                    FROM event_stats
                    WHERE event_slug = %s
                    LIMIT 1
                    """,
                    (event_slug,),
                )
                row = cur.fetchone()
                if row:
                    existing_stats = {
                        "event_date": row[0],
                        "event_display_name": row[1],
                        "startgg_snapshot": row[2],
                    }

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
            selected_dates: List[str] = []
            all_dates: List[str] = []

            for ev in events:
                if not isinstance(ev, dict):
                    continue
                ev_slug = str(ev.get("event_slug") or ev.get("slug") or "").strip()
                for key in date_keys:
                    parsed = _coerce_to_iso_date(ev.get(key))
                    if parsed:
                        all_dates.append(parsed)
                        if ev_slug and ev_slug == slug:
                            selected_dates.append(parsed)

            # Prefer exact slug match when available, otherwise earliest event date in snapshot.
            if selected_dates:
                return min(selected_dates)
            if all_dates:
                return min(all_dates)

        return None

    active_slug = (settings or {}).get("active_event_slug") if isinstance(settings, dict) else None
    settings_match_slug = bool(active_slug and str(active_slug) == str(event_slug))

    if not event_date:
        if settings:
            ed = settings.get("event_date") if settings_match_slug else None
            if ed:
                event_date = ed.isoformat() if hasattr(ed, "isoformat") else str(ed)

        if not event_date and existing_stats:
            ed = existing_stats.get("event_date")
            if ed:
                event_date = ed.isoformat() if hasattr(ed, "isoformat") else str(ed)

        if not event_date:
            snapshot_for_date = startgg_snapshot
            if snapshot_for_date is None and settings and settings_match_slug:
                snapshot_for_date = settings.get("events_json")
            if snapshot_for_date is None and existing_stats:
                snapshot_for_date = existing_stats.get("startgg_snapshot")
            event_date = _extract_date_from_snapshot(snapshot_for_date, event_slug)

        if not event_date:
            event_date = datetime.now(timezone.utc).date().isoformat()
            logger.warning(
                "archive_event: missing event_date in payload/settings/snapshot for '%s'; "
                "falling back to today's date (%s)",
                event_slug,
                event_date,
            )

    # Normalize resolved/provided event_date to ISO date string.
    normalized_event_date = _coerce_to_iso_date(event_date)
    if not normalized_event_date:
        raise ValueError(f"Invalid event_date for '{event_slug}': {event_date}")
    event_date = normalized_event_date

    if not event_display_name and settings and settings_match_slug:
        event_display_name = settings.get("event_display_name") or ""

    if not event_display_name and existing_stats:
        event_display_name = str(existing_stats.get("event_display_name") or "")

    if not swish_expected_per_game and settings:
        swish_expected_per_game = settings.get("swish_expected_per_game") or 0

    if startgg_snapshot is None and settings and settings_match_slug:
        startgg_snapshot = settings.get("events_json")

    if startgg_snapshot is None and existing_stats:
        startgg_snapshot = existing_stats.get("startgg_snapshot")

    now = datetime.now(timezone.utc)
    replaced_rows = 0

    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (event_slug,))
                # Player totals and merge/undo snapshots must not interleave.
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('fgt:player-merges'))")

                # 1. Read active checkins for this slug
                cur.execute(
                    """
                    SELECT record_id, name, tag, email, telephone, status,
                           member, member_source, member_lookup_state, startgg, payment_valid,
                           payment_amount, payment_expected,
                           tournament_games_registered, checkin_uuid,
                           external_id, startgg_event_id, is_guest, added_via, acquisition_source
                    FROM active_event_data
                    WHERE event_slug = %s
                    ORDER BY record_id
                    FOR UPDATE
                    """,
                    (event_slug,),
                )
                columns = [desc[0] for desc in cur.description]
                raw_rows = cur.fetchall()

                if not raw_rows:
                    raise ArchiveError("no active rows")

                checkins = [_row_to_dict(columns, row) for row in raw_rows]
                archived_record_ids = [row["record_id"] for row in checkins]
                cur.execute(
                    """SELECT count(*) FROM event_archive
                       WHERE event_slug = %s AND source_record_id = ANY(%s)""",
                    (event_slug, archived_record_ids),
                )
                replaced_rows = cur.fetchone()[0]

                # 2. Match/create players (returns uuid + new/returning flag)
                player_results = []
                for c in checkins:
                    p_uuid, is_new = _match_or_create_player(cur, c, event_slug, event_date, now)
                    player_results.append((p_uuid, is_new))

                # 3. Insert into event_archive (with player_uuid)
                cur.executemany(
                    """
                    INSERT INTO event_archive (
                        event_slug, event_date, event_display_name, source_record_id,
                        name, tag, email, telephone, status,
                        member, startgg, payment_valid,
                        payment_amount, payment_expected,
                        swish_expected_per_game,
                        tournament_games_registered, checkin_uuid,
                        external_id, startgg_event_id, is_guest, added_via, acquisition_source,
                        archived_at, player_uuid, member_source, member_lookup_state
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s,
                        %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (event_slug, source_record_id)
                        WHERE source_record_id IS NOT NULL
                    DO UPDATE SET
                        event_date = EXCLUDED.event_date,
                        event_display_name = EXCLUDED.event_display_name,
                        name = EXCLUDED.name, tag = EXCLUDED.tag,
                        email = EXCLUDED.email, telephone = EXCLUDED.telephone,
                        status = EXCLUDED.status, member = EXCLUDED.member,
                        member_source = EXCLUDED.member_source,
                        member_lookup_state = EXCLUDED.member_lookup_state,
                        startgg = EXCLUDED.startgg, payment_valid = EXCLUDED.payment_valid,
                        payment_amount = EXCLUDED.payment_amount,
                        payment_expected = EXCLUDED.payment_expected,
                        swish_expected_per_game = EXCLUDED.swish_expected_per_game,
                        tournament_games_registered = EXCLUDED.tournament_games_registered,
                        checkin_uuid = EXCLUDED.checkin_uuid, external_id = EXCLUDED.external_id,
                        startgg_event_id = EXCLUDED.startgg_event_id, is_guest = EXCLUDED.is_guest,
                        added_via = EXCLUDED.added_via,
                        acquisition_source = EXCLUDED.acquisition_source,
                        archived_at = EXCLUDED.archived_at, player_uuid = EXCLUDED.player_uuid
                    """,
                    [
                        (
                            event_slug,
                            event_date,
                            event_display_name,
                            c["record_id"],
                            c.get("name"),
                            c.get("tag"),
                            c.get("email"),
                            c.get("telephone"),
                            c.get("status"),
                            c.get("member"),
                            c.get("startgg"),
                            c.get("payment_valid"),
                            c.get("payment_amount"),
                            c.get("payment_expected"),
                            swish_expected_per_game,
                            c.get("tournament_games_registered"),
                            c.get("checkin_uuid"),
                            c.get("external_id"),
                            c.get("startgg_event_id"),
                            c.get("is_guest"),
                            c.get("added_via"),
                            c.get("acquisition_source"),
                            now,
                            player_results[i][0],
                            c.get("member_source"),
                            c.get("member_lookup_state"),
                        )
                        for i, c in enumerate(checkins)
                    ],
                )

                # 4. Snapshot requirements only for a new archive of the matching active event.
                # Historic metadata is not backfilled from today's unrelated settings.
                cur.execute(
                    "SELECT require_payment, require_membership, require_startgg FROM settings "
                    "WHERE is_active = true AND active_event_slug = %s LIMIT 1",
                    (event_slug,),
                )
                requirement_row = cur.fetchone()
                requirements = None
                if requirement_row is not None:
                    requirements = dict(zip(
                        ("require_payment", "require_membership", "require_startgg"), requirement_row
                    ))
                cur.execute(
                    "INSERT INTO event_stats (event_slug, event_date, event_display_name, archived_at, "
                    "startgg_snapshot, requirements_snapshot) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (event_slug) DO UPDATE SET event_date = EXCLUDED.event_date, "
                    "event_display_name = EXCLUDED.event_display_name, startgg_snapshot = EXCLUDED.startgg_snapshot",
                    (
                        event_slug,
                        event_date,
                        event_display_name,
                        now,
                        Json(startgg_snapshot) if startgg_snapshot is not None else None,
                        Json(requirements) if requirements is not None else None,
                    ),
                )
                computed = recompute_event_stats(event_slug, user=user, _conn=conn, _audit=False)
                stats = computed["after"]
                new_players = stats["new_players"]
                returning_players = stats["returning_players"]
                retention_rate = stats["retention_rate"]
                startgg_registered_count = stats["startgg_registered_count"]
                startgg_registered_players = stats["startgg_registered_players"]
                checked_in_count = stats["checked_in_count"]
                no_show_count = stats["no_show_count"]
                no_show_rate = stats["no_show_rate"]
                integrity_warnings = computed["integrity_warnings"]
                recompute_player_aggregates(dry_run=False, _conn=conn, _audit=False)

                # 6. Optionally clear active data
                deleted_count = 0
                if clear_active:
                    cur.execute(
                        "DELETE FROM active_event_data WHERE record_id = ANY(%s)",
                        (archived_record_ids,),
                    )
                    deleted_count = cur.rowcount

                    # Also reset active_event_slug in settings so the cleared
                    # event no longer appears as "active" in the select-event
                    # dropdown or forces stale dashboard state.
                    cur.execute(
                        """
                        UPDATE settings
                        SET active_event_slug = NULL,
                            event_display_name = NULL,
                            tournament_name = NULL
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
        details=json.dumps(
            {
                "participants": stats["total_participants"],
                "total_revenue": str(stats["total_revenue"]),
                "new_players": new_players,
                "returning_players": returning_players,
                "replaced_rows": replaced_rows,
                "cleared_active": clear_active,
                "integrity_warnings": integrity_warnings,
            }
        ),
    )

    logger.info(
        f"📦 Archived event '{event_slug}': "
        f"{stats['total_participants']} participants, "
        f"revenue {stats['total_revenue']}"
    )

    return {
        "event_slug": event_slug,
        "archived": len(checkins),
        "total_revenue": float(stats["total_revenue"]),
        "avg_payment": float(stats["avg_payment"]),
        "member_count": stats["member_count"],
        "guest_count": stats["guest_count"],
        "startgg_count": stats["startgg_count"],
        "new_players": new_players,
        "returning_players": returning_players,
        "retention_rate": float(retention_rate) if retention_rate is not None else None,
        "games_breakdown": stats["games_breakdown"],
        "most_popular_game": stats["most_popular_game"],
        "status_breakdown": stats["status_breakdown"],
        "startgg_registered_count": startgg_registered_count,
        "checked_in_count": checked_in_count,
        "no_show_count": no_show_count,
        "no_show_rate": float(no_show_rate) if no_show_rate is not None else None,
        "integrity_warnings": integrity_warnings,
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
                    "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (event_slug,)
                )
                cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
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
                    cur.execute(
                        "INSERT INTO settings (is_active, created_at, updated_at) VALUES (true, now(), now()) RETURNING id"
                    )
                    settings_id = cur.fetchone()[0]

                event_date_value = (
                    event_date.isoformat()
                    if event_date is not None and hasattr(event_date, "isoformat")
                    else event_date
                )

                cur.execute(
                    """
                    UPDATE settings
                    SET is_active = true,
                        active_event_slug = %s,
                        event_display_name = NULLIF(%s, ''),
                        tournament_name = NULLIF(%s, ''),
                        event_date = COALESCE(%s::date, event_date),
                        startgg_event_url = NULL,
                        events_json = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        event_slug,
                        event_display_name or "",
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
                            "SELECT count(*) FROM event_archive WHERE event_slug = %s",
                            (event_slug,),
                        )
                        expected_rows = cur.fetchone()[0]
                        cur.execute(
                            """
                            INSERT INTO active_event_data (
                                record_id, event_slug, external_id,
                                name, tag, email, telephone,
                                status, member, startgg, payment_valid,
                                payment_amount, payment_expected,
                                tournament_games_registered, checkin_uuid,
                                startgg_event_id, is_guest, added_via, acquisition_source, player_uuid,
                                created, member_source, member_lookup_state
                            )
                            SELECT
                                COALESCE(source_record_id, gen_random_uuid()::text),
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
                                acquisition_source,
                                player_uuid,
                                %s, member_source, member_lookup_state
                            FROM event_archive
                            WHERE event_slug = %s
                            ON CONFLICT (record_id) DO NOTHING
                            """,
                            (now, event_slug),
                        )
                        restored_rows = cur.rowcount or 0
                        if restored_rows != expected_rows:
                            raise ValueError("Cannot restore check-in IDs already in use")

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
            cur.execute(
                """
                SELECT uuid, name, tag, email, telephone, total_events
                FROM players
                ORDER BY total_events DESC, name
            """
            )
            players = cur.fetchall()
            dismissed = _dismissed_pair_keys(cur)

    # Build lookup structures
    player_list = []
    for row in players:
        p_uuid, name, tag, email, telephone, total_events = row
        player_list.append(
            {
                "uuid": p_uuid,
                "name": (name or "").strip(),
                "tag": (tag or "").strip(),
                "email": (email or "").strip(),
                "telephone": (telephone or "").strip(),
                "total_events": total_events or 0,
            }
        )

    seen_pairs = set()

    for i, a in enumerate(player_list):
        for b in player_list[i + 1 :]:
            pair_key = tuple(sorted([a["uuid"], b["uuid"]]))
            # Skip before the limit check, so dismissed pairs never use up candidate slots.
            if pair_key in seen_pairs or pair_key in dismissed:
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
                candidates.append(
                    {
                        "player_a": a,
                        "player_b": b,
                        "reasons": reasons,
                        "confidence": confidence,
                    }
                )

            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    # Sort by confidence (high first), then by number of reasons
    conf_order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: (conf_order.get(c["confidence"], 3), -len(c["reasons"])))

    logger.info(f"🔍 Found {len(candidates)} duplicate candidates")
    return candidates


# ---------------------------------------------------------------------------
# Duplicate dismissals ("Not same", T-056)
# ---------------------------------------------------------------------------
DISMISSALS_TABLE = "player_duplicate_dismissals"


def ordered_duplicate_pair(uuid_a: str, uuid_b: str) -> tuple:
    """Return the stored (smaller, larger) form of a pair; reject empty or identical uuids."""
    a = (uuid_a or "").strip()
    b = (uuid_b or "").strip()
    if not a or not b or a == b:
        raise ValueError("A dismissal needs two different player uuids")
    return (a, b) if a < b else (b, a)


def _dismissals_table_exists(cur) -> bool:
    # Startup migrations can be disabled (FGT_MIGRATIONS_ENABLED=0); the scan must still work.
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (DISMISSALS_TABLE,))
    return bool(cur.fetchone()[0])


def _dismissed_pair_keys(cur) -> set:
    if not _dismissals_table_exists(cur):
        logger.warning("player_duplicate_dismissals is missing; showing all duplicate pairs")
        return set()
    cur.execute("SELECT uuid_a, uuid_b FROM player_duplicate_dismissals")
    return {(row[0], row[1]) for row in cur.fetchall()}


def dismiss_duplicate_pair(uuid_a: str, uuid_b: str, dismissed_by: str = "") -> bool:
    """Persist "these two are different players". Idempotent.

    Returns True when the pair is (now) dismissed, False when either player no longer
    exists (e.g. merged meanwhile). Takes the global player-merge lock shared, so it
    serializes against merge/undo (exclusive) like every other player writer.
    """
    a, b = ordered_duplicate_pair(uuid_a, uuid_b)
    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
                cur.execute("SELECT count(*) FROM players WHERE uuid IN (%s, %s)", (a, b))
                if cur.fetchone()[0] != 2:
                    return False
                cur.execute(
                    """
                    INSERT INTO player_duplicate_dismissals (uuid_a, uuid_b, dismissed_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (uuid_a, uuid_b) DO NOTHING
                    """,
                    (a, b, (dismissed_by or "")[:120]),
                )
    return True


def undo_duplicate_dismissal(
    uuid_a: str, uuid_b: str, *, user: Optional[Dict[str, Any]] = None
) -> bool:
    """Remove and audit a dismissal atomically. False when no row was removed."""
    a, b = ordered_duplicate_pair(uuid_a, uuid_b)
    actor = user or {}
    actor_id = str(actor.get("user_id") or "")
    actor_name = str(actor.get("user_name") or "").strip()
    if not actor_name or "@" in actor_name:
        actor_name = f"user-{actor_id}" if actor_id else "system"
    with _get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock_shared(hashtext('fgt:player-merges'))")
                cur.execute(
                    "DELETE FROM player_duplicate_dismissals WHERE uuid_a = %s AND uuid_b = %s",
                    (a, b),
                )
                if not cur.rowcount:
                    return False
                # Record only the trusted actor's id/name and the two profile identifiers.
                cur.execute(
                    "INSERT INTO audit_log "
                    "(user_id, user_name, action, target_table, target_player, reason, details) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        actor_id,
                        actor_name,
                        "duplicate_dismissal_undone",
                        "players",
                        a,
                        "TO undid 'not same player'",
                        json.dumps({"pair": [a, b]}),
                    ),
                )
                return True


def is_duplicate_pair_dismissed(uuid_a: str, uuid_b: str) -> bool:
    a, b = ordered_duplicate_pair(uuid_a, uuid_b)
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            if not _dismissals_table_exists(cur):
                return False
            cur.execute(
                "SELECT 1 FROM player_duplicate_dismissals WHERE uuid_a = %s AND uuid_b = %s",
                (a, b),
            )
            return cur.fetchone() is not None


def _dismissal_player(player_uuid, name, tag) -> Dict[str, str]:
    return {"uuid": player_uuid, "name": (name or "").strip(), "tag": (tag or "").strip()}


_LIVE_DISMISSALS = """
    FROM player_duplicate_dismissals d
    JOIN players pa ON pa.uuid = d.uuid_a
    JOIN players pb ON pb.uuid = d.uuid_b
"""


def count_duplicate_dismissals() -> int:
    """How many dismissed pairs still have both players (the true "Dismissed pairs (N)")."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            if not _dismissals_table_exists(cur):
                return 0
            cur.execute("SELECT count(*) " + _LIVE_DISMISSALS)
            return int(cur.fetchone()[0])


def get_dismissed_pair_keys() -> set:
    """Every stored (uuid_a, uuid_b) pair, for reconciling the cards on screen."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            return _dismissed_pair_keys(cur)


def get_duplicate_dismissals(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Dismissed pairs whose players both still exist, newest first (names/tags only)."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            if not _dismissals_table_exists(cur):
                return []
            cur.execute(
                """
                SELECT d.uuid_a, d.uuid_b, d.dismissed_at, d.dismissed_by,
                       pa.name, pa.tag, pb.name, pb.tag
                """
                + _LIVE_DISMISSALS
                + """
                ORDER BY d.dismissed_at DESC, d.id DESC
                LIMIT %s OFFSET %s
                """,
                (limit, max(0, offset)),
            )
            rows = cur.fetchall()
    return [
        {
            "uuid_a": row[0],
            "uuid_b": row[1],
            "dismissed_at": row[2].isoformat() if row[2] else "",
            "dismissed_by": row[3] or "",
            "player_a": _dismissal_player(row[0], row[4], row[5]),
            "player_b": _dismissal_player(row[1], row[6], row[7]),
        }
        for row in rows
    ]


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
                cur.execute("SET LOCAL lock_timeout = '10s'")
                for lock_attempt in range(3):
                    # Retry only the lock phase if an incoming check-in adds a new event.
                    cur.execute("SAVEPOINT player_event_locks")
                    cur.execute(
                        "SELECT DISTINCT event_slug FROM active_event_data "
                        "WHERE player_uuid = ANY(%s)",
                        ([keep_uuid, remove_uuid],),
                    )
                    locked_slugs = sorted(row[0] for row in cur.fetchall())
                    for slug in locked_slugs:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (slug,)
                        )
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext('fgt:player-merges'))")
                    cur.execute(
                        "SELECT DISTINCT event_slug FROM active_event_data "
                        "WHERE player_uuid = ANY(%s)",
                        ([keep_uuid, remove_uuid],),
                    )
                    if any(row[0] not in locked_slugs for row in cur.fetchall()):
                        cur.execute("ROLLBACK TO SAVEPOINT player_event_locks")
                        cur.execute("RELEASE SAVEPOINT player_event_locks")
                        continue
                    cur.execute("RELEASE SAVEPOINT player_event_locks")
                    break
                else:
                    raise ValueError(
                        "Active check-ins changed during lock acquisition; retry the merge"
                    )
                cur.execute("SELECT * FROM players WHERE uuid = %s FOR UPDATE", (keep_uuid,))
                keep_row = cur.fetchone()
                if not keep_row:
                    raise ValueError(f"Keep player not found: {keep_uuid}")
                keep_cols = [desc[0] for desc in cur.description]
                keep_player = _row_to_dict(keep_cols, keep_row)

                cur.execute("SELECT * FROM players WHERE uuid = %s FOR UPDATE", (remove_uuid,))
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
                    "UPDATE event_archive SET player_uuid = %s WHERE player_uuid = %s RETURNING id",
                    (keep_uuid, remove_uuid),
                )
                moved_archive_ids = sorted(row[0] for row in cur.fetchall())
                archive_updated = len(moved_archive_ids)

                # 4. Re-point active_event_data rows
                cur.execute(
                    "UPDATE active_event_data SET player_uuid = %s WHERE player_uuid = %s RETURNING record_id",
                    (keep_uuid, remove_uuid),
                )
                moved_active_ids = sorted(row[0] for row in cur.fetchall())
                active_updated = len(moved_active_ids)

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

                # A "not same" decision belongs to these profiles, not a different survivor.
                if _dismissals_table_exists(cur):
                    cur.execute(
                        "DELETE FROM player_duplicate_dismissals WHERE uuid_a = %s OR uuid_b = %s",
                        (remove_uuid, remove_uuid),
                    )

                # 6. Delete the removed player
                cur.execute("DELETE FROM players WHERE uuid = %s", (remove_uuid,))

                # 7. Write to merge_log
                cur.execute(
                    "SELECT md5(to_jsonb(p)::text) FROM players p WHERE uuid = %s",
                    (keep_uuid,),
                )
                after_state = {"player": cur.fetchone()[0]}
                cur.execute(
                    """SELECT id, md5(to_jsonb(a)::text) FROM event_archive a
                       WHERE player_uuid = %s ORDER BY id FOR UPDATE""",
                    (keep_uuid,),
                )
                after_state["archive"] = [list(row) for row in cur.fetchall()]
                cur.execute(
                    """SELECT record_id, md5(to_jsonb(a)::text) FROM active_event_data a
                       WHERE player_uuid = %s ORDER BY record_id FOR UPDATE""",
                    (keep_uuid,),
                )
                after_state["active"] = [list(row) for row in cur.fetchall()]
                before_totals = {"keep": keep_player, "remove": remove_player, "after": after_state}
                cur.execute(
                    """
                    INSERT INTO merge_log (
                        keep_uuid, remove_uuid,
                        user_id, user_name, reason,
                        removed_player_snapshot,
                        archive_rows_updated, active_rows_updated,
                        moved_archive_ids, moved_active_ids, before_totals
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        moved_archive_ids,
                        moved_active_ids,
                        _Json(before_totals, dumps=lambda value: json.dumps(value, default=str)),
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
        details=json.dumps(
            {
                "merge_id": merge_id,
                "keep_uuid": keep_uuid,
                "remove_uuid": remove_uuid,
                "archive_rows_updated": archive_updated,
                "active_rows_updated": active_updated,
            }
        ),
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
    """Undo only the newest unchanged merge using exact row IDs and player snapshots."""
    from psycopg import sql
    from psycopg.types.json import Json as _Json

    try:
        merge_id = int(merge_id)
    except (TypeError, ValueError):
        raise ValueError("Invalid merge id") from None
    undo_user = user or {"user_id": "", "user_name": "system", "user_email": ""}

    with _get_pool().connection() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute("SELECT keep_uuid, remove_uuid FROM merge_log WHERE id = %s", (merge_id,))
            players = cur.fetchone()
            if not players:
                raise ValueError("Merge log entry not found")
            for lock_attempt in range(3):
                cur.execute("SAVEPOINT player_event_locks")
                cur.execute(
                    "SELECT DISTINCT event_slug FROM active_event_data "
                    "WHERE player_uuid = ANY(%s)",
                    (list(players),),
                )
                locked_slugs = sorted(row[0] for row in cur.fetchall())
                for slug in locked_slugs:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('fgt-event:' || %s))", (slug,)
                    )
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('fgt:player-merges'))")
                cur.execute(
                    "SELECT DISTINCT event_slug FROM active_event_data WHERE player_uuid = ANY(%s)",
                    (list(players),),
                )
                if any(row[0] not in locked_slugs for row in cur.fetchall()):
                    cur.execute("ROLLBACK TO SAVEPOINT player_event_locks")
                    cur.execute("RELEASE SAVEPOINT player_event_locks")
                    continue
                cur.execute("RELEASE SAVEPOINT player_event_locks")
                break
            else:
                raise ValueError("Active check-ins changed during lock acquisition; retry the undo")
            cur.execute("SELECT * FROM merge_log WHERE id = %s FOR UPDATE", (merge_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Merge log entry not found")
            entry = _row_to_dict([column[0] for column in cur.description], row)
            if entry.get("undone"):
                raise ValueError("Merge has already been undone")
            cur.execute("SELECT id FROM merge_log ORDER BY id DESC LIMIT 1")
            if cur.fetchone()[0] != merge_id:
                raise ValueError("Only the newest merge can be undone")

            keep_uuid, remove_uuid = entry["keep_uuid"], entry["remove_uuid"]
            before = entry.get("before_totals")
            moved_archive_ids = entry.get("moved_archive_ids")
            moved_active_ids = entry.get("moved_active_ids")
            if (
                not isinstance(before, dict)
                or not all(isinstance(before.get(key), dict) for key in ("keep", "remove", "after"))
                or not isinstance(moved_archive_ids, list)
                or not isinstance(moved_active_ids, list)
            ):
                raise ValueError("Exact undo metadata is unavailable for this merge")
            keep_before, snapshot, after = before["keep"], before["remove"], before["after"]
            if keep_before.get("uuid") != keep_uuid or snapshot.get("uuid") != remove_uuid:
                raise ValueError("Merge snapshots do not match the players")

            cur.execute(
                """SELECT uuid, md5(to_jsonb(p)::text) FROM players p
                   WHERE uuid = ANY(%s) ORDER BY uuid FOR UPDATE""",
                ([keep_uuid, remove_uuid],),
            )
            if cur.fetchall() != [(keep_uuid, after.get("player"))]:
                raise ValueError("Players changed after this merge")
            cur.execute(
                """SELECT id, md5(to_jsonb(a)::text) FROM event_archive a
                   WHERE player_uuid = ANY(%s) ORDER BY id FOR UPDATE""",
                ([keep_uuid, remove_uuid],),
            )
            if [list(row) for row in cur.fetchall()] != after.get("archive"):
                raise ValueError("Player archive rows changed after this merge")
            cur.execute(
                """SELECT record_id, md5(to_jsonb(a)::text) FROM active_event_data a
                   WHERE player_uuid = ANY(%s) ORDER BY record_id FOR UPDATE""",
                ([keep_uuid, remove_uuid],),
            )
            if [list(row) for row in cur.fetchall()] != after.get("active"):
                raise ValueError("Player active rows changed after this merge")
            cur.execute("SELECT 1 FROM players WHERE id = %s", (snapshot.get("id"),))
            if cur.fetchone() is not None:
                raise ValueError("Removed player row identifier is no longer available")

            def adapt(column, value):
                if column in {"game_counts", "events_list", "member_history"} and value is not None:
                    return _Json(value)
                return value

            columns = list(snapshot)
            cur.execute(
                sql.SQL("INSERT INTO players ({}) VALUES ({})").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                ),
                [adapt(column, snapshot[column]) for column in columns],
            )
            columns = [column for column in keep_before if column not in {"id", "uuid"}]
            cur.execute(
                sql.SQL("UPDATE players SET {} WHERE uuid = %s").format(
                    sql.SQL(", ").join(
                        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in columns
                    )
                ),
                [*[adapt(column, keep_before[column]) for column in columns], keep_uuid],
            )
            cur.execute(
                "UPDATE event_archive SET player_uuid = %s WHERE id = ANY(%s) AND player_uuid = %s",
                (remove_uuid, moved_archive_ids, keep_uuid),
            )
            archive_reverted = cur.rowcount
            cur.execute(
                "UPDATE active_event_data SET player_uuid = %s WHERE record_id = ANY(%s) AND player_uuid = %s",
                (remove_uuid, moved_active_ids, keep_uuid),
            )
            active_reverted = cur.rowcount
            if archive_reverted != len(moved_archive_ids) or active_reverted != len(
                moved_active_ids
            ):
                raise ValueError("Moved rows no longer match the merge")
            cur.execute(
                "UPDATE merge_log SET undone = true, undone_at = now() WHERE id = %s", (merge_id,)
            )

    log_action(
        undo_user,
        "player_merge_undone",
        "players",
        target_player=remove_uuid,
        details=json.dumps(
            {
                "merge_id": merge_id,
                "keep_uuid": keep_uuid,
                "remove_uuid": remove_uuid,
                "archive_reverted": archive_reverted,
                "active_reverted": active_reverted,
            }
        ),
    )
    logger.info(
        "Undid merge #%s (archive=%s, active=%s)", merge_id, archive_reverted, active_reverted
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
            mid,
            merged_at,
            keep_uuid,
            remove_uuid,
            user_name,
            reason,
            snapshot,
            archive_updated,
            active_updated,
            undone,
            undone_at,
        ) = row

        # Extract display names from snapshot
        removed_name = snapshot.get("name", "") if isinstance(snapshot, dict) else ""
        removed_tag = snapshot.get("tag", "") if isinstance(snapshot, dict) else ""

        result.append(
            {
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
            }
        )

    return result

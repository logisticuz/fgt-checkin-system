-- FGC Check-in System - Postgres Schema
-- Runs automatically on first container start via docker-entrypoint-initdb.d
--
-- Sources:
--   docs/event-history/06-final-spec.md (authoritative spec)
--   shared/airtable_tables_backup/*.csv (existing Airtable fields)

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================
-- settings - Active event configuration
-- =============================================
CREATE TABLE settings (
    id                      SERIAL PRIMARY KEY,
    is_active               BOOLEAN DEFAULT false,
    active_event_slug       TEXT,
    event_display_name      TEXT,
    startgg_event_url       TEXT,
    tournament_name         TEXT,
    timezone                TEXT,
    event_date              DATE,
    events_json             JSONB,
    default_game            TEXT[],
    startgg_event_ids       TEXT,                    -- multiline text (Airtable legacy)
    webhook_discord         TEXT,
    swish_number            TEXT,
    swish_expected_per_game INTEGER DEFAULT 0,
    require_payment         BOOLEAN DEFAULT false,
    require_membership      BOOLEAN DEFAULT false,
    require_startgg         BOOLEAN DEFAULT false,
    offer_membership        BOOLEAN DEFAULT false,
    created_at              TIMESTAMPTZ DEFAULT now(),
    updated_at              TIMESTAMPTZ DEFAULT now()
);

-- =============================================
-- active_event_data - Live check-ins
-- =============================================
CREATE TABLE active_event_data (
    id                              SERIAL PRIMARY KEY,
    record_id                       TEXT UNIQUE NOT NULL DEFAULT gen_random_uuid()::text,
    event_slug                      TEXT NOT NULL,
    external_id                     TEXT,
    name                            TEXT,
    tag                             TEXT,
    email                           TEXT,
    telephone                       TEXT,
    status                          TEXT DEFAULT 'Pending',
    member                          BOOLEAN DEFAULT false,
    startgg                         BOOLEAN DEFAULT false,
    payment_valid                   BOOLEAN DEFAULT false,
    payment_amount                  NUMERIC(10,2) DEFAULT 0,
    payment_expected                NUMERIC(10,2) DEFAULT 0,
    tournament_games_registered     TEXT[],
    checkin_uuid                    TEXT,            -- maps to Airtable "UUID"
    startgg_event_id                TEXT,
    is_guest                        BOOLEAN DEFAULT false,
    player_uuid                     TEXT,
    created                         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_active_event_slug ON active_event_data(event_slug);
CREATE INDEX idx_active_tag ON active_event_data(LOWER(tag));
CREATE INDEX idx_active_name ON active_event_data(LOWER(name));
CREATE INDEX idx_active_player_uuid ON active_event_data(player_uuid);

-- =============================================
-- event_archive - Archived check-ins per event
-- (renamed from event_history)
-- =============================================
CREATE TABLE event_archive (
    id                              SERIAL PRIMARY KEY,
    event_slug                      TEXT NOT NULL,
    event_date                      DATE,
    event_display_name              TEXT,
    -- Player data (snapshot from active_event_data at archive time)
    name                            TEXT,
    tag                             TEXT,
    email                           TEXT,
    telephone                       TEXT,
    status                          TEXT,
    member                          BOOLEAN DEFAULT false,
    startgg                         BOOLEAN DEFAULT false,
    payment_valid                   BOOLEAN DEFAULT false,
    payment_amount                  NUMERIC(10,2) DEFAULT 0,
    payment_expected                NUMERIC(10,2) DEFAULT 0,
    swish_expected_per_game         INTEGER,
    tournament_games_registered     TEXT[],          -- renamed from games_registered
    checkin_uuid                    TEXT,            -- maps to Airtable "UUID"
    external_id                     TEXT,
    startgg_event_id                TEXT,
    is_guest                        BOOLEAN DEFAULT false,
    -- Archive metadata
    archived_at                     TIMESTAMPTZ DEFAULT now(),
    player_uuid                     TEXT,
    added_manually                  BOOLEAN DEFAULT false,
    manual_add_reason               TEXT,
    notes                           TEXT
);

CREATE INDEX idx_archive_event_slug ON event_archive(event_slug);
CREATE INDEX idx_archive_player_uuid ON event_archive(player_uuid);

-- =============================================
-- event_stats - Aggregated statistics per event
-- =============================================
CREATE TABLE event_stats (
    id                  SERIAL PRIMARY KEY,
    event_slug          TEXT UNIQUE NOT NULL,
    event_date          DATE,
    event_display_name  TEXT,
    archived_at         TIMESTAMPTZ,
    -- Totals
    total_participants  INTEGER DEFAULT 0,
    total_revenue       NUMERIC(10,2) DEFAULT 0,
    avg_payment         NUMERIC(10,2) DEFAULT 0,
    -- Segments
    member_count        INTEGER DEFAULT 0,
    member_percentage   NUMERIC(5,2) DEFAULT 0,
    guest_count         INTEGER DEFAULT 0,
    startgg_count       INTEGER DEFAULT 0,
    -- Retention
    new_players         INTEGER DEFAULT 0,
    returning_players   INTEGER DEFAULT 0,
    retention_rate      NUMERIC(5,2) DEFAULT 0,
    -- Breakdowns
    games_breakdown     JSONB,
    most_popular_game   TEXT,
    status_breakdown    JSONB,
    -- Snapshot
    startgg_snapshot    JSONB,
    -- No-show tracking
    startgg_registered_count  INTEGER DEFAULT 0,
    checked_in_count          INTEGER DEFAULT 0,
    no_show_count             INTEGER DEFAULT 0,
    no_show_rate              NUMERIC(5,2) DEFAULT 0
);

-- =============================================
-- players - Persistent player profiles
-- =============================================
CREATE TABLE players (
    id              SERIAL PRIMARY KEY,
    uuid            TEXT UNIQUE NOT NULL DEFAULT gen_random_uuid()::text,
    name            TEXT,
    tag             TEXT,
    email           TEXT,
    telephone       TEXT,
    -- Stats
    games_played    TEXT[],
    total_events    INTEGER DEFAULT 0,              -- renamed from events_participated
    total_paid      NUMERIC(10,2) DEFAULT 0,
    -- Games detail
    favorite_game   TEXT,
    game_counts     JSONB,
    -- Timeline
    first_seen      TIMESTAMPTZ,                    -- renamed from joined_at
    last_seen       TIMESTAMPTZ,
    first_event     TEXT,
    last_event      TEXT,
    -- History
    events_list     JSONB,
    -- Membership
    is_member       BOOLEAN DEFAULT false,
    member_history  JSONB,
    -- Meta
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_players_uuid ON players(uuid);
CREATE INDEX idx_players_tag ON players(LOWER(tag));
CREATE INDEX idx_players_name ON players(LOWER(name));

-- =============================================
-- sessions - Auth sessions (Start.gg OAuth)
-- =============================================
CREATE TABLE sessions (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT UNIQUE NOT NULL,
    user_id         TEXT NOT NULL,
    user_name       TEXT,
    user_email      TEXT,
    access_token    TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_active     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_sessions_session_id ON sessions(session_id);

-- =============================================
-- audit_log - Traceability for all actions
-- =============================================
CREATE TABLE audit_log (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT now(),
    user_id         TEXT,
    user_name       TEXT,
    user_email      TEXT,
    action          TEXT NOT NULL,
    target_table    TEXT,
    target_event    TEXT,
    target_record   TEXT,
    target_player   TEXT,
    reason          TEXT,
    details         TEXT,
    before_state    JSONB,
    after_state     JSONB
);

CREATE INDEX idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_user ON audit_log(user_id);

-- =============================================
-- merge_log - Player merge history (undo-capable)
-- =============================================
CREATE TABLE merge_log (
    id                  SERIAL PRIMARY KEY,
    merged_at           TIMESTAMPTZ DEFAULT now(),
    keep_uuid           TEXT NOT NULL,
    remove_uuid         TEXT NOT NULL,
    user_id             TEXT,
    user_name           TEXT,
    reason              TEXT,
    -- Snapshot of removed player before deletion (enables undo)
    removed_player_snapshot JSONB NOT NULL,
    -- Which tables/rows were updated
    archive_rows_updated    INTEGER DEFAULT 0,
    active_rows_updated     INTEGER DEFAULT 0,
    undone                  BOOLEAN DEFAULT false,
    undone_at               TIMESTAMPTZ
);

CREATE INDEX idx_merge_log_keep ON merge_log(keep_uuid);
CREATE INDEX idx_merge_log_remove ON merge_log(remove_uuid);

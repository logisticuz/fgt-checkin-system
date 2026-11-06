-- Membership secrets are encrypted or keyed digests; receipts and sends have no attempt FK.
CREATE TABLE IF NOT EXISTS membership_attempts (
    id UUID PRIMARY KEY,
    checkin_id TEXT,
    event_slug TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'code_sent', 'submitting', 'submitted', 'failed', 'outcome_unknown', 'locked', 'expired',
        'manual_review'
    )),
    pnr_enc BYTEA,
    context_enc BYTEA,
    pnr_fingerprint TEXT,
    channel TEXT NOT NULL CHECK (channel IN ('sms', 'email')),
    contact_fingerprint TEXT NOT NULL,
    code_hash TEXT,
    code_sent_at TIMESTAMPTZ NOT NULL,
    code_expires_at TIMESTAMPTZ NOT NULL,
    verify_attempts INTEGER NOT NULL DEFAULT 0 CHECK (verify_attempts BETWEEN 0 AND 5),
    submit_started_at TIMESTAMPTZ,
    last_reconciled_at TIMESTAMPTZ,
    outcome_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CHECK (expires_at = created_at + interval '15 minutes')
);
CREATE INDEX IF NOT EXISTS membership_attempts_state ON membership_attempts (state);
CREATE INDEX IF NOT EXISTS membership_attempts_pnr_state
    ON membership_attempts (pnr_fingerprint, state);
CREATE INDEX IF NOT EXISTS membership_attempts_contact_sent
    ON membership_attempts (contact_fingerprint, code_sent_at);
CREATE UNIQUE INDEX IF NOT EXISTS membership_attempts_one_inflight_pnr
    ON membership_attempts (pnr_fingerprint)
    WHERE state IN ('submitting', 'outcome_unknown', 'manual_review');

CREATE TABLE IF NOT EXISTS membership_receipts (
    attempt_id UUID PRIMARY KEY,
    year INTEGER NOT NULL,
    bylaws_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL,
    consented_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    channel TEXT NOT NULL CHECK (channel IN ('sms', 'email')),
    outcome TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS membership_code_sends (
    id BIGSERIAL PRIMARY KEY,
    attempt_id UUID NOT NULL,
    contact_fingerprint TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS membership_code_sends_contact_sent
    ON membership_code_sends (contact_fingerprint, sent_at);

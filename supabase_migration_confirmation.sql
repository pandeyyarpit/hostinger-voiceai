-- ══════════════════════════════════════════════════════════════════════════════
-- APPOINTMENT CONFIRMATION AGENT — Run once in Supabase SQL Editor
-- These tables intentionally have no anon/authenticated access. The FastAPI
-- server and confirmation worker use SUPABASE_SERVICE_ROLE_KEY from .env.
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS confirmation_settings (
    id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    enabled             BOOLEAN NOT NULL DEFAULT FALSE,
    lead_hours          INTEGER NOT NULL DEFAULT 24 CHECK (lead_hours BETWEEN 1 AND 720),
    timezone            TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    call_window_start   TIME NOT NULL DEFAULT '09:00',
    call_window_end     TIME NOT NULL DEFAULT '19:00',
    max_attempts        SMALLINT NOT NULL DEFAULT 2 CHECK (max_attempts BETWEEN 1 AND 5),
    retry_delay_minutes INTEGER NOT NULL DEFAULT 120 CHECK (retry_delay_minutes BETWEEN 5 AND 10080),
    script              TEXT NOT NULL DEFAULT '',
    voice               TEXT NOT NULL DEFAULT 'kavya',
    language            TEXT NOT NULL DEFAULT 'hi-IN',
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO confirmation_settings (id)
VALUES (TRUE)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS appointments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cal_booking_id      TEXT NOT NULL UNIQUE,
    patient_name        TEXT,
    patient_phone       TEXT NOT NULL,
    patient_email       TEXT,
    starts_at           TIMESTAMPTZ NOT NULL,
    booking_status      TEXT NOT NULL DEFAULT 'booked'
                        CHECK (booking_status IN ('booked', 'cancelled', 'reschedule_requested')),
    confirmation_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (confirmation_status IN ('pending', 'queued', 'calling', 'confirmed', 'cancelled', 'reschedule_requested', 'no_answer', 'failed', 'skipped')),
    confirmed_at        TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_appointments_confirmation_schedule
    ON appointments (starts_at, confirmation_status)
    WHERE booking_status = 'booked';

CREATE TABLE IF NOT EXISTS contact_preferences (
    patient_phone        TEXT PRIMARY KEY,
    confirmation_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    opted_out_at          TIMESTAMPTZ,
    opt_out_reason        TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS confirmation_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    appointment_id      UUID NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    attempt_number      SMALLINT NOT NULL CHECK (attempt_number BETWEEN 1 AND 5),
    trigger_type        TEXT NOT NULL DEFAULT 'automatic'
                        CHECK (trigger_type IN ('automatic', 'manual')),
    scheduled_for       TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'calling', 'confirmed', 'cancelled', 'reschedule_requested', 'no_answer', 'failed', 'skipped')),
    livekit_room        TEXT,
    call_log_id         UUID,
    outcome_detail      TEXT,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (appointment_id, attempt_number)
);

-- At most one queued or calling job exists for the appointment at any time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_confirmation_jobs_one_active_per_appointment
    ON confirmation_jobs (appointment_id)
    WHERE status IN ('queued', 'calling');

CREATE INDEX IF NOT EXISTS idx_confirmation_jobs_due
    ON confirmation_jobs (scheduled_for)
    WHERE status = 'queued';

ALTER TABLE confirmation_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE appointments ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE confirmation_jobs ENABLE ROW LEVEL SECURITY;

-- Do not expose patient confirmation data through the public Data API.
REVOKE ALL ON confirmation_settings, appointments, contact_preferences, confirmation_jobs
    FROM anon, authenticated;

-- ══════════════════════════════════════════════════════════════════════════════
-- DONE. Leave the Confirmation Agent toggle OFF until the worker is configured
-- and the controlled live-call test has passed.
-- ══════════════════════════════════════════════════════════════════════════════
